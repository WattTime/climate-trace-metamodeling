import datetime
import json

import numpy as np
import pandas as pd
import psycopg2 as pg2

from gap_filling.utils import parse_and_format_query_data

INSERT_MAPPING = {"ois": "original_inventory_sector", "pen": "producing_entity_name",
                  "pei": "producing_entity_id", "re": "reporting_entity", "epf": "emitted_product_formula",
                  "eq": "emission_quantity", "equ": "emission_quantity_units",
                  "st": "start_time", "et": "end_time"}


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
            curs.execute("SELECT original_inventory_sector, producing_entity_name, producing_entity_id, reporting_entity, "
                         "emitted_product_formula, emission_quantity, emission_quantity_units, start_time "
                         "FROM ermin WHERE reporting_entity = %s AND start_time >= %s "
                         "AND carbon_equivalency_method IS NULL",
                         (source, start_date))
        else:
            if not is_co2e:
                curs.execute("SELECT original_inventory_sector, producing_entity_name, producing_entity_id, reporting_entity, "
                             "emitted_product_formula, emission_quantity, emission_quantity_units, start_time "
                             "FROM ermin WHERE reporting_entity = %s AND emitted_product_formula = %s "
                             "AND start_time >= %s AND carbon_equivalency_method IS NULL",
                             (source, gas, start_date))
            else:
                curs.execute("SELECT original_inventory_sector, producing_entity_name, producing_entity_id, reporting_entity, "
                             "emitted_product_formula, emission_quantity, emission_quantity_units, start_time "
                             "FROM ermin WHERE reporting_entity = %s AND emitted_product_formula = %s "
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

    def write_data(self, data_to_insert, rows_type=None):
        # Two different kinds of inserts we'll need to perform here
        if rows_type == "climate-trace":
            INSERT_MAPPING["cem"] = "carbon_equivalency_method"
            insert_str = "INSERT INTO ermin (original_inventory_sector, producing_entity_name, producing_entity_id, " \
                         "reporting_entity, emitted_product_formula, emission_quantity, emission_quantity_units, " \
                         "carbon_equivalency_method, start_time, end_time) " \
                         "VALUES (%(ois)s, %(pen)s, %(pei)s, %(re)s, %(epf)s, %(eq)s, %(equ)s, %(cem)s, %(st)s, %(et)s)"

        elif rows_type == "edgar":
            INSERT_MAPPING["mmd"] = "measurement_method_doi_or_url"
            insert_str = "INSERT INTO ermin (original_inventory_sector, producing_entity_name, producing_entity_id, " \
                         "reporting_entity, emitted_product_formula, emission_quantity, emission_quantity_units, " \
                         "measurement_method_doi_or_url, start_time, end_time) " \
                         "VALUES (%(ois)s, %(pen)s, %(pei)s, %(re)s, %(epf)s, %(eq)s, %(equ)s, %(mmd)s, %(st)s, %(et)s)"

        curs = self.get_cursor()

        # According to the docs, executemany() is no faster than execute() in a loop, so running it in a loop
        for dti in data_to_insert.iterrows():
            data_row_dict = {k: dti[1][INSERT_MAPPING[k]] for k in INSERT_MAPPING.keys()}
            curs.execute(insert_str, data_row_dict)
            self.conn.commit()
