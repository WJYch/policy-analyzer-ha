#!/usr/bin/env python3
"""Fetch electricity price data from InfluxDB → price_data.json → git push to GH Pages.

Usage:
  python3 fetch_price_data.py              # fetch all provinces
  python3 fetch_price_data.py --province 江苏  # single province

Output: price_data.json (in same dir as index.html)
"""

import json, os, sys, subprocess
from datetime import datetime, timezone, timedelta
from influxdb_client import InfluxDBClient

# === CONFIG ===
INFLUX_URL = "http://192.168.1.89:8086"
TOKEN = "ToFZ-ewNYaj_m09su2dFb2EKJAAOX3k5nK0Wy00fS46gcItE7R24EBJb_UhKYmXCCkUoVZ1XQKX9H4e_pDcooA=="
ORG = "shenshu"
OUTPUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "price_data.json")

BJT = timezone(timedelta(hours=8))

def to_bjt_str(dt):
    """Convert UTC timestamp to BJT datetime string."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(BJT).strftime("%Y-%m-%d %H:%M")

def to_bjt_date(dt):
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(BJT).strftime("%Y-%m-%d")

def fetch_spot(client, province, days=7):
    """Fetch day_ahead and real_time spot prices for a province."""
    result = {"province": province, "type": "spot", "day_ahead": [], "real_time": []}
    qapi = client.query_api()
    
    for price_type in ("day_ahead", "real_time"):
        bucket = f"spot_price_{province}"
        flux = f'''from(bucket: "{bucket}")
  |> range(start: -{days}d)
  |> filter(fn: (r) => r["priceType"] == "{price_type}")
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
  |> sort(columns: ["_time"])'''
        
        try:
            tables = qapi.query(flux)
            records = []
            for table in tables:
                for r in table.records:
                    records.append({
                        "t": to_bjt_str(r["_time"]),
                        "p": round(float(r["price"]), 1)
                    })
            if price_type == "day_ahead":
                result["day_ahead"] = records
            else:
                result["real_time"] = records
        except Exception as e:
            result[f"{price_type}_error"] = str(e)
    
    return result

def fetch_agent_price(client, province, months=24):
    """Fetch monthly agent purchasing price."""
    bucket = f"agent_price_{province}"
    flux = f'''from(bucket: "{bucket}")
  |> range(start: -{months}mo)
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
  |> sort(columns: ["_time"])'''
    
    result = {"province": province, "type": "agent", "records": []}
    try:
        tables = client.query_api().query(flux)
        seen = set()
        for table in tables:
            for r in table.records:
                month_key = to_bjt_date(r["_time"])
                if month_key in seen:
                    continue
                seen.add(month_key)
                rec = {"t": month_key}
                for field in ("purchasingPrice", "lineLossCost", "purchasingSystemOperatingCost", "purchasingSum"):
                    try:
                        rec[field] = round(float(r[field]), 4)
                    except (KeyError, TypeError):
                        pass
                result["records"].append(rec)
        result["records"].sort(key=lambda x: x["t"])
    except Exception as e:
        result["error"] = str(e)
    return result

def fetch_mid_long_term(client, province, months=24):
    """Fetch monthly mid/long term price."""
    bucket = f"mid_long_term_price_{province}"
    flux = f'''from(bucket: "{bucket}")
  |> range(start: -{months}mo)
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
  |> sort(columns: ["_time"])'''
    
    result = {"province": province, "type": "mid_long", "records": []}
    try:
        tables = client.query_api().query(flux)
        seen = set()
        for table in tables:
            for r in table.records:
                month_key = to_bjt_date(r["_time"])
                if month_key in seen:
                    continue
                seen.add(month_key)
                rec = {"t": month_key, "p": round(float(r["price"]), 1)}
                result["records"].append(rec)
        result["records"].sort(key=lambda x: x["t"])
    except Exception as e:
        result["error"] = str(e)
    return result

def main():
    provinces_arg = sys.argv[1:] if len(sys.argv) > 1 else []
    # Phase 1: just Jiangsu
    provinces = [p for p in provinces_arg if not p.startswith("--")] or ["江苏"]
    
    client = InfluxDBClient(url=INFLUX_URL, token=TOKEN, org=ORG, timeout=30_000)
    
    data = {
        "generated_at": datetime.now(BJT).strftime("%Y-%m-%d %H:%M:%S"),
        "spot": [],
        "agent": [],
        "mid_long": []
    }
    
    for prov in provinces:
        print(f"  Fetching spot_{prov}...")
        data["spot"].append(fetch_spot(client, prov))
        print(f"  Fetching agent_{prov}...")
        data["agent"].append(fetch_agent_price(client, prov))
        print(f"  Fetching mid_long_{prov}...")
        data["mid_long"].append(fetch_mid_long_term(client, prov))
    
    client.close()
    
    with open(OUTPUT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=None)
    
    size = os.path.getsize(OUTPUT)
    print(f"\n✅ {OUTPUT} ({size/1024:.1f} KB)")
    
    # Count
    for s in data["spot"]:
        print(f"  现货 {s['province']}: day_ahead={len(s['day_ahead'])} real_time={len(s['real_time'])}")
    for a in data["agent"]:
        print(f"  代理购电 {a['province']}: {len(a['records'])} 个月")
    for m in data["mid_long"]:
        print(f"  中长期 {m['province']}: {len(m['records'])} 条")
    
    # Git push
    repo = os.path.dirname(os.path.abspath(__file__))
    try:
        subprocess.run(["git", "-C", repo, "add", "price_data.json"], check=True, capture_output=True)
        subprocess.run(["git", "-C", repo, "commit", "-m", f"price_data: update {data['generated_at']}"], check=True, capture_output=True)
        subprocess.run(["git", "-C", repo, "push"], check=True, capture_output=True)
        print("  ✅ git push done")
    except subprocess.CalledProcessError as e:
        if "nothing to commit" in e.stderr.decode():
            print("  ℹ️  No changes to commit")
        else:
            print(f"  ⚠️  git error: {e.stderr.decode()}")

if __name__ == "__main__":
    main()
