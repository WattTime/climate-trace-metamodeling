import datetime
import json
import logging

import time
import numpy as np
import pandas as pd
import psycopg2 as pg2

from gap_filling.utils import parse_and_format_query_data

INSERT_MAPPING = {"ois": "original_inventory_sector",
                  "i3c": "iso3_country", "re": "reporting_entity", "g": "gas",
                  "eq": "emissions_quantity", "equ": "emissions_quantity_units",
                  "st": "start_time", "et": "end_time", "cd": "created_date"}


def init_db_connect(params):
    try:
        conn = pg2.connect(user=params['db_user'],
                           password=params['db_pass'],
                           host="rds-climate-trace.watttime.org",
                           database="climatetrace")
    except Exception as e:
        print(e)
        raise Exception(e)

    # Double check in case somehow there was a silent error or issue
    if not conn:
        raise ConnectionError("Initializing the database connection failed!")
    return conn


class DataHandler:
    def __init__(self, params_file='params.json'):
        self.params_file = params_file

        self.conn = init_db_connect(self.get_params())

    def get_params(self):
        with open(self.params_file, 'r') as fid:
            params = json.load(fid)
        return params

    def get_cursor(self):
        if not self.conn:
            raise ConnectionError("No database connection has been established!")
        if self.conn.closed:
            raise ConnectionError("The database connection is closed!")

        return self.conn.cursor()

    def load_data(self, source: str, years_to_columns=False, rename_columns=True, gas=None, is_co2e=False,
                  start_date=datetime.date(2013, 1, 1)):
        curs = self.get_cursor()

        if gas is None:
            curs.execute("SELECT original_inventory_sector, reporting_entity, iso3_country, "
                         "gas, emissions_quantity, emissions_quantity_units, start_time "
                         "FROM country_emissions WHERE reporting_entity = %s AND start_time >= %s "
                         "AND gas != 'co2e_100yr' AND gas != 'co2e_20yr'",
                         (source, start_date))
        else:
            if not is_co2e:
                curs.execute("SELECT original_inventory_sector, reporting_entity, "
                             "gas, emissions_quantity, emissions_quantity_units, start_time "
                             "FROM country_emissions WHERE reporting_entity = %s AND gas = %s "
                             "AND start_time >= %s AND gas != 'co2e_100yr' AND gas != 'co2e_20yr'",
                             (source, gas, start_date))
            else:
                curs.execute("SELECT original_inventory_sector, reporting_entity, "
                             "gas, emissions_quantity, emissions_quantity_units, start_time "
                             "FROM country_emissions WHERE reporting_entity = %s AND gas = %s "
                             "AND start_time >= %s",
                             (source, gas, start_date))

        colnames = [desc[0] for desc in curs.description]
        return parse_and_format_query_data(pd.DataFrame(data=np.array(curs.fetchall()), columns=np.array(colnames)),
                                           years_to_columns=years_to_columns, rename_columns=rename_columns)

    def get_ghgs(self, f_gas=None):
        curs = self.get_cursor()
        if f_gas:
            curs.execute("SELECT * FROM ghgs WHERE f_gas_category = %s", (f_gas,))
        elif f_gas == 'all':
            curs.execute("SELECT * FROM ghgs WHERE f_gas_category is NOT NULL")
        else:
            curs.execute("SELECT lower_designation as gas, gwp_20yr as co2e_20, gwp_100yr as co2e_100 FROM ghgs")

        colnames = [desc[0] for desc in curs.description]
        return pd.DataFrame(data=np.array(curs.fetchall()), columns=colnames)

    def insert_with_update(self, e_data, table):
        data_dict = e_data.to_dict('records')
        with self.get_cursor() as cur:
            cs = 1000
            for i in range(0, len(data_dict), cs):
                logging.warning(f'inserting row {i} of {len(data_dict)}')
                data = data_dict[i:i+cs]
                vals = []

                for d in data:
                    vshort = ()
                    for k in d.keys():
                        vshort += (d[k],)
                    vals.append(vshort)

                base_select = data[0].copy()
                base_where = data[0].copy()
                base_and = data[0].copy()

                for x in ['created_date']:
                    base_select.pop(x)

                for x in ['created_date', 'emissions_quantity']:
                    base_where.pop(x)

                for x in ['created_date']:
                    base_and.pop('created_date')

                set_statement = ""
                for d, v in data[0].items():
                    if d == 'created_date':
                        continue
                    set_statement += f" {d} = excluded.{d},"
                set_statement += f" modified_date = NOW()"

                # build where statement
                where_statement = ""
                for d, v in base_where.items():
                    if d == 'location':
                        where_statement += f" ST_AsText({table}.{d}) = ST_AsText(excluded.{d}) AND"
                    else:
                        where_statement += f" {table}.{d} = excluded.{d} AND"
                where_statement = where_statement[:-3]

                # build and statement
                and_statement = "("
                for d, v in base_and.items():
                    if d == 'location':
                        and_statement += f" ST_AsText({table}.{d}) != ST_AsText(excluded.{d}) OR"
                    elif d == 'emissions_quantity':
                        and_statement += f" coalesce({table}.{d}, 0.0) != excluded.{d} OR"
                    else:
                        and_statement += f" {table}.{d} != excluded.{d} OR"
                and_statement = and_statement[:-2] + ')'

                sss = ','.join('%s' for s in range(len(list(data[0].keys()))))
                args_str = ','.join(cur.mogrify(f"({sss})", x).decode("utf-8") for x in vals)
                insert_str = f"""INSERT INTO {table} ({','.join(data[0].keys())})
                                                 VALUES {args_str}
                                                 ON CONFLICT ON CONSTRAINT country_duplicates DO UPDATE 
                                                 SET {set_statement}
                                                 WHERE {where_statement}
                                                 AND {and_statement}
                                         """
                cur.execute(insert_str, vals)
                self.conn.commit()

    def write_data(self, data_to_insert, rows_type=None):
        # Two different kinds of inserts we'll need to perform here
        data_to_insert['created_date'] = datetime.datetime.now().isoformat()
        if rows_type == "climate-trace":
            #INSERT_MAPPING["cem"] = "carbon_equivalency_method"
            insert_str = "INSERT INTO country_emissions (original_inventory_sector, iso3_country, " \
                         "reporting_entity, gas, emissions_quantity, emissions_quantity_units, " \
                         "start_time, end_time, created_date) " \
                         "VALUES (%(ois)s, %(i3c)s, %(re)s, %(g)s, %(eq)s, %(equ)s, %(st)s, %(et)s, %(cd)s)"

        # TODO: how do we store this data (the measurement method thing)?
        elif rows_type == "edgar":
            INSERT_MAPPING["mmd"] = "measurement_method_doi_or_url"
            insert_str = "INSERT INTO country_emissions (original_inventory_sector, iso3_country, " \
                         "reporting_entity, gas, emissions_quantity, emissions_quantity_units, " \
                         "measurement_method_doi_or_url, start_time, end_time) " \
                         "VALUES (%(ois)s, %(i3c)s, %(re)s, %(g)s, %(eq)s, %(equ)s, %(mmd)s, %(st)s, %(et)s)"

        curs = self.get_cursor()

        # According to the docs, executemany() is no faster than execute() in a loop, so running it in a loop
        for dti in data_to_insert.iterrows():
            print(f'inserting row {dti[0]} out of {data_to_insert.shape[0]} for sector {data_to_insert.loc[dti[0], "original_inventory_sector"]}')
            data_row_dict = {k: dti[1][INSERT_MAPPING[k]] for k in INSERT_MAPPING.keys()}
            try:
                curs.execute(insert_str, data_row_dict)
            except Exception as e:
                t = e.pgerror
                if 'duplicate' in t.split(':')[1]:
                    print('Duplicate encountered, skipping entry')
                    continue
            self.conn.commit()

