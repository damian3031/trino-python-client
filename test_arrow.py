from trino.dbapi import connect

from timeit import default_timer as timer

conn = connect(
    host="localhost",
    port=57077,
    user="trino_arrow",
    catalog="tpch",
    schema="tiny",
    http_headers={"X-Trino-Client-Capabilities": "ARROW_RESULTS"},
)
cur = conn.cursor()
start_arrow = timer()
cur.execute("select * from tpch.sf1.orders")
df = cur.fetch_pandas_all()
end_arrow = timer()
print(f"time arrow fetch_pandas_all: {end_arrow - start_arrow}")
print(df)
