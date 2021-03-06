from redis import Redis

from jx_bigquery import bigquery
from jx_mysql.mysql import MySQL, quote_value
from jx_mysql.mysql_snowflake_extractor import MySqlSnowflakeExtractor
from mo_files import File
from mo_json import json2value, value2json
from mo_logs import Log, constants, startup
from mo_sql import SQL
from mo_times import DAY, YEAR, Timer
from mo_times.dates import Date
from treeherder.config.settings import REDIS_URL

CONFIG_FILE = (File.new_instance(__file__).parent / "extract_alerts.json").abspath


class ExtractAlerts:
    def run(self, force=False, restart=False, merge=False):
        try:
            # SETUP LOGGING
            settings = startup.read_settings(default_filename=CONFIG_FILE)
            constants.set(settings.constants)
            Log.start(settings.debug)

            self.extract(settings, force, restart, merge)
        except Exception as e:
            Log.error("could not extract alerts", cause=e)
        finally:
            Log.stop()

    def extract(self, settings, force, restart, merge):
        if not settings.extractor.app_name:
            Log.error("Expecting an extractor.app_name in config file")

        # SETUP DESTINATION
        destination = bigquery.Dataset(
            dataset=settings.extractor.app_name, kwargs=settings.destination
        ).get_or_create_table(settings.destination)

        try:
            if merge:
                with Timer("merge shards"):
                    destination.merge_shards()

            # RECOVER LAST SQL STATE
            redis = Redis.from_url(REDIS_URL)
            state = redis.get(settings.extractor.key)

            if restart or not state:
                state = (0, 0)
                redis.set(settings.extractor.key, value2json(state).encode("utf8"))
            else:
                state = json2value(state.decode("utf8"))

            last_modified, alert_id = state
            last_modified = Date(last_modified)

            # SCAN SCHEMA, GENERATE EXTRACTION SQL
            extractor = MySqlSnowflakeExtractor(settings.source)
            canonical_sql = extractor.get_sql(SQL("SELECT 0"))

            # ENSURE SCHEMA HAS NOT CHANGED SINCE LAST RUN
            old_sql = redis.get(settings.extractor.sql)
            if old_sql and old_sql.decode("utf8") != canonical_sql.sql:
                if force:
                    Log.warning("Schema has changed")
                else:
                    Log.error("Schema has changed")
            redis.set(settings.extractor.sql, canonical_sql.sql.encode("utf8"))

            # SETUP SOURCE
            source = MySQL(settings.source.database)

            while True:
                Log.note(
                    "Extracting alerts for last_modified={{last_modified|datetime|quote}}, alert.id={{alert_id}}",
                    last_modified=last_modified,
                    alert_id=alert_id,
                )
                last_year = Date.today() - YEAR + DAY  # ONLY YOUNG RECORDS CAN GO INTO BIGQUERY

                get_ids = SQL(
                    "SELECT s.id "
                    + "\nFROM treeherder.performance_alert_summary s"
                    + "\nLEFT JOIN treeherder.performance_alert a ON s.id=a.summary_id"
                    + "\nWHERE s.created>"
                    + quote_value(last_year).sql
                    + " AND (s.last_updated > "
                    + quote_value(last_modified).sql
                    + "\nOR a.last_updated > "
                    + quote_value(last_modified).sql
                    + ")"
                    + "\nGROUP BY s.id"
                    + "\nORDER BY s.id"
                    + "\nLIMIT "
                    + quote_value(settings.extractor.chunk_size).sql
                )
                sql = extractor.get_sql(get_ids)

                # PULL FROM source, AND PUSH TO destination
                acc = []
                with source.transaction():
                    cursor = source.query(sql, stream=True, row_tuples=True)
                    extractor.construct_docs(cursor, acc.append, False)
                if not acc:
                    break
                for r in acc:
                    r.etl.timestamp = Date.now()
                destination.extend(acc)

                # RECORD THE STATE
                last_doc = acc[-1]
                last_modified, alert_id = last_doc.created, last_doc.id
                redis.set(
                    settings.extractor.key, value2json((last_modified, alert_id)).encode("utf8"),
                )

                if len(acc) < settings.extractor.chunk_size:
                    break

        except Exception as e:
            Log.warning("problem with extraction", cause=e)

        Log.note("done alert extraction")

        try:
            with Timer("merge shards"):
                destination.merge_shards()
        except Exception as e:
            Log.warning("problem with merge", cause=e)

        Log.note("done alert merge")
        Log.stop()
