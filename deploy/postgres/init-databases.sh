#!/bin/sh
# 首次初始化数据卷时创建测试库，使 pytest 与开发库互不覆盖。
# 仅在 postgres 镜像的空数据卷首次启动时执行。
set -eu

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname template1 <<SQL
CREATE DATABASE "$TEST_DATABASE" OWNER "$POSTGRES_USER";
SQL
