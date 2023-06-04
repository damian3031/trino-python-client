--- ARROW vs JSON ---
SELECT * FROM tpch.sf10.orders limit 5000000
-- time arrow fetch_pandas_all: 29.57s
-- time json read_sql: 42.86s
-- boost: ~45%

SELECT * FROM tpch.sf10.orders
-- time arrow fetch_pandas_all: 100.60s
-- time json read_sql: 213.63s
-- boost: ~110%
