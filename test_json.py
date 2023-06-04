from timeit import default_timer as timer

from sqlalchemy import create_engine
import pandas as pd


engine = create_engine("trino://trino_arrow@localhost:57077/user")
start = timer()
df = pd.read_sql("SELECT * FROM tpch.sf1.orders", engine)
end = timer()
print(f"time json read_sql: {end - start}")
print(df)
