#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_ec.py —— 电商订单日终审计（2026-11-01 至 2026-11-10）

输入
----
orders.csv : 一单一行，字段 订单号,支付时间,发货时间,完成时间,应付金额,优惠金额,状态
             时间格式 %Y-%m-%d %H:%M:%S，尚未发生的时间留空；金额单位：分
items.csv  : 子行，字段 订单号,商品号,数量,单价；金额单位：分

两套校验
--------
1. 金额勾稽：应付金额 + 优惠金额 == Σ(子行数量 × 子行单价)，不满足则勾稽不过。
2. 状态链：生命周期 待支付→已支付→已发货→已完成（已完成后可→已退款），
   待支付/已支付两个阶段可→已取消。各状态要求的时间存在性：
       待支付 (无,无,无)   已支付 (有,无,无)   已发货 (有,有,无)
       已完成 (有,有,有)   已取消 (有,无,无)   已退款 (有,有,有)
   且已存在的时间须满足 支付<=发货<=完成，任一不符即状态断链。

审计范围（按支付时间落窗）
--------------------------
* 支付时间在 2026-11-01~2026-11-10 之外的单（10 月底、11-11 等）整单排除；
* 无支付时间且状态为「待支付」的单属正常未支付，排除；
* 无支付时间但状态已推进的单（支付时间尚未落进来）属断链，保留并进补录登记。

登记路由（两套校验独立运行，不互斥）
------------------------------------
* 勾稽不过            -> dispute_orders（争议登记）
* 状态断链            -> supplement_orders（补录登记）
  两缺陷叠加在同一笔上的，两张登记表各记一行；
* 两套校验都通过      -> settled_orders（日结），并按支付日汇总进 daily_summary。

输出：SQLite 库 audit.db 四张表（重复执行先清空再重算，各登记不翻倍）
  dispute_orders(order_id)
  supplement_orders(order_id)
  settled_orders(order_id)
  daily_summary(settle_date, order_count, total_amount)   -- 金额单位：分
"""

import argparse
import csv
import sqlite3
from collections import defaultdict
from datetime import date, datetime

WINDOW_START = date(2026, 11, 1)
WINDOW_END = date(2026, 11, 10)
DT_FMT = "%Y-%m-%d %H:%M:%S"

# 状态 -> (支付时间, 发货时间, 完成时间) 各自是否必须存在
STATUS_TIME_RULE = {
    "待支付": (False, False, False),
    "已支付": (True, False, False),
    "已发货": (True, True, False),
    "已完成": (True, True, True),
    "已取消": (True, False, False),
    "已退款": (True, True, True),
}


def parse_dt(value):
    """空串 -> None；否则按约定格式解析为 datetime。"""
    value = (value or "").strip()
    return datetime.strptime(value, DT_FMT) if value else None


def load_orders(path):
    orders = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            orders.append(
                {
                    "order_id": row["订单号"].strip(),
                    "pay_dt": parse_dt(row["支付时间"]),
                    "ship_dt": parse_dt(row["发货时间"]),
                    "done_dt": parse_dt(row["完成时间"]),
                    "payable": int(row["应付金额"]),
                    "discount": int(row["优惠金额"]),
                    "status": row["状态"].strip(),
                }
            )
    return orders


def load_item_totals(path):
    """汇总每个订单的子行数量×单价之和（分）。"""
    totals = defaultdict(int)
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            totals[row["订单号"].strip()] += int(row["数量"]) * int(row["单价"])
    return totals


def in_scope(order):
    """支付日落窗；无支付时间时，仅非正常待支付的断链单保留。"""
    if order["pay_dt"] is not None:
        return WINDOW_START <= order["pay_dt"].date() <= WINDOW_END
    return order["status"] != "待支付"


def chain_broken(order):
    """状态链校验：时间存在性与时间顺序，任一不符返回 True。"""
    rule = STATUS_TIME_RULE[order["status"]]
    times = (order["pay_dt"], order["ship_dt"], order["done_dt"])
    presence = tuple(t is not None for t in times)
    if presence != rule:
        return True
    pay_dt, ship_dt, done_dt = times
    if pay_dt is not None and ship_dt is not None and ship_dt < pay_dt:
        return True
    if ship_dt is not None and done_dt is not None and done_dt < ship_dt:
        return True
    return False


def amount_mismatch(order, item_totals):
    """勾稽校验：应付 + 优惠 应等于子行合计。"""
    gross = item_totals.get(order["order_id"], 0)
    return order["payable"] + order["discount"] != gross


def audit(orders_path, items_path):
    orders = load_orders(orders_path)
    item_totals = load_item_totals(items_path)

    dispute, supplement, settled = [], [], []
    for order in orders:
        if not in_scope(order):
            continue
        bad_amount = amount_mismatch(order, item_totals)
        bad_chain = chain_broken(order)
        if bad_amount:
            dispute.append(order["order_id"])
        if bad_chain:
            supplement.append(order["order_id"])
        if not bad_amount and not bad_chain:
            settled.append(order)

    # 日结按支付日汇总（进日结的单必有窗口内支付时间）
    summary = defaultdict(lambda: [0, 0])
    for order in settled:
        day = order["pay_dt"].date().isoformat()
        summary[day][0] += 1
        summary[day][1] += order["payable"]
    daily_summary = [
        (day, cnt, amount) for day, (cnt, amount) in sorted(summary.items())
    ]
    return sorted(dispute), sorted(supplement), sorted(o["order_id"] for o in settled), daily_summary


def write_db(db_path, dispute, supplement, settled, daily_summary):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS dispute_orders (order_id TEXT PRIMARY KEY)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS supplement_orders (order_id TEXT PRIMARY KEY)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS settled_orders (order_id TEXT PRIMARY KEY)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS daily_summary ("
            "settle_date TEXT PRIMARY KEY, order_count INTEGER, total_amount INTEGER)"
        )
        # 整批重算，保证重复执行结果不翻倍
        with conn:
            for table in (
                "dispute_orders",
                "supplement_orders",
                "settled_orders",
                "daily_summary",
            ):
                conn.execute(f"DELETE FROM {table}")
            conn.executemany(
                "INSERT INTO dispute_orders(order_id) VALUES (?)",
                [(x,) for x in dispute],
            )
            conn.executemany(
                "INSERT INTO supplement_orders(order_id) VALUES (?)",
                [(x,) for x in supplement],
            )
            conn.executemany(
                "INSERT INTO settled_orders(order_id) VALUES (?)",
                [(x,) for x in settled],
            )
            conn.executemany(
                "INSERT INTO daily_summary(settle_date, order_count, total_amount) VALUES (?, ?, ?)",
                daily_summary,
            )
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description="电商订单 11/01-11/10 日终审计")
    parser.add_argument("--orders", default="/workspace/orders.csv", help="orders.csv 路径")
    parser.add_argument("--items", default="/workspace/items.csv", help="items.csv 路径")
    parser.add_argument("--db", default="/workspace/audit.db", help="SQLite 输出库路径")
    args = parser.parse_args()

    dispute, supplement, settled, daily_summary = audit(args.orders, args.items)
    write_db(args.db, dispute, supplement, settled, daily_summary)

    overlap = sorted(set(dispute) & set(supplement))
    print(f"争议登记 dispute_orders     : {len(dispute)} 单")
    print(f"补录登记 supplement_orders  : {len(supplement)} 单")
    print(f"日结   settled_orders       : {len(settled)} 单")
    print(f"双重缺陷（同时进两张登记表）: {len(overlap)} 单 {overlap}")
    print("daily_summary:")
    total_count = total_amount = 0
    for day, cnt, amount in daily_summary:
        total_count += cnt
        total_amount += amount
        print(f"  {day}  单数={cnt:<4d} 应付合计={amount}")
    print(f"  合计  单数={total_count}  应付合计={total_amount}")


if __name__ == "__main__":
    main()
