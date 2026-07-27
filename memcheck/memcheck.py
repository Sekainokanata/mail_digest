"""Ollama のホストメモリ増加を切り分けるための計測スクリプト。

使い方(例):
    pip install psutil ollama
    python memcheck.py --n 12 --mode vary --num-batch 512      # ベースライン
    python memcheck.py --n 12 --mode vary --num-batch 64       # num_batch 仮説の検証
    python memcheck.py --n 12 --mode same --num-batch 512      # プロンプトキャッシュ仮説の検証
    python memcheck.py --n 12 --mode vary --body-len 200       # 入力長依存の検証

各リクエストの前後で ollama 関連プロセスのメモリを記録し、
1リクエストあたりの増加量をプロセス別・カウンタ別に出力する。
"""
import argparse
import csv
import random
import string
import time

import ollama
import psutil

# 監視対象プロセス名(小文字・部分一致)
TARGET_HINTS = ("ollama", "llama-server", "llama_server", "ggml")


def find_targets():
    """ollama / llama-server 系のプロセスを列挙する。"""
    procs = []
    for p in psutil.process_iter(["pid", "name"]):
        try:
            name = (p.info["name"] or "").lower()
            if any(h in name for h in TARGET_HINTS):
                procs.append(p)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return procs


def snapshot():
    """プロセスごとに RSS(working set) と private(commit) を MB 単位で返す。"""
    snap = {}
    for p in find_targets():
        try:
            mi = p.memory_info()
            # Windows では rss=Working Set, private=Private Bytes(コミット)
            private = getattr(mi, "private", None)
            snap[(p.pid, p.name())] = {
                "rss_mb": mi.rss / 1024 ** 2,
                "private_mb": (private / 1024 ** 2) if private is not None else float("nan"),
            }
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return snap


def system_committed_mb():
    """システム全体のコミット量(概算)。"""
    vm = psutil.virtual_memory()
    return (vm.total - vm.available) / 1024 ** 2


def totals(snap):
    return (
        sum(v["rss_mb"] for v in snap.values()),
        sum(v["private_mb"] for v in snap.values()),
    )


BASE_PROMPT = """あなたはメール秘書です。以下のメールが「重要」かどうかを判定してください。
重要と判定する基準の例:
- 締切や期限のある依頼・連絡
- 請求書・契約・支払いに関する連絡
- 緊急対応が必要な内容

差出人: {sender}
件名: {subject}
本文:
{body}

以下のJSON形式のみを出力してください。
{{"important": true または false, "reason": "一文で"}}
"""


def make_prompt(i: int, mode: str, body_len: int) -> str:
    if mode == "same":
        # 完全に同一のプロンプト。プロンプトキャッシュがヒットする条件。
        sender, subject, seed = "a@example.com", "件名テスト", "あ"
    else:
        # 毎回異なるプロンプト。実アプリと同じ条件。
        rnd = "".join(random.choices(string.ascii_lowercase, k=8))
        sender, subject, seed = f"{rnd}@example.com", f"件名{rnd}", rnd
    body = (seed * ((body_len // max(len(seed), 1)) + 1))[:body_len]
    return BASE_PROMPT.format(sender=sender, subject=subject, body=body)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="gemma4:12b-it-qat")
    ap.add_argument("--n", type=int, default=12, help="リクエスト回数")
    ap.add_argument("--mode", choices=["same", "vary"], default="vary")
    ap.add_argument("--num-batch", type=int, default=None, help="未指定なら Ollama の既定(512)")
    ap.add_argument("--num-ctx", type=int, default=4096)
    ap.add_argument("--body-len", type=int, default=3000, help="本文の文字数")
    ap.add_argument("--csv", default="memcheck.csv")
    args = ap.parse_args()

    options = {"num_ctx": args.num_ctx, "num_predict": 128, "temperature": 0}
    if args.num_batch is not None:
        options["num_batch"] = args.num_batch

    print(f"model={args.model} mode={args.mode} body_len={args.body_len} options={options}")
    print("モデルをロード中...")
    ollama.chat(
        model=args.model,
        messages=[{"role": "user", "content": "ping"}],
        keep_alive="30m",
        options=options,
    )
    time.sleep(3.0)  # ロード直後の揺らぎを落ち着かせる

    base = snapshot()
    base_rss, base_priv = totals(base)
    base_sys = system_committed_mb()
    print(f"\n基準値: ollama系 RSS={base_rss:,.0f}MB  Private={base_priv:,.0f}MB  "
          f"システム使用={base_sys:,.0f}MB")
    print(f"監視対象プロセス: {[f'{n}(pid={p})' for p, n in base.keys()]}\n")

    rows = []
    print(f"{'#':>3} {'RSS計':>10} {'ΔRSS':>9} {'Private計':>11} {'ΔPriv':>9} {'システム':>10} {'Δsys':>9}")
    print("-" * 68)

    prev_rss, prev_priv, prev_sys = base_rss, base_priv, base_sys
    for i in range(1, args.n + 1):
        prompt = make_prompt(i, args.mode, args.body_len)
        try:
            ollama.chat(
                model=args.model,
                messages=[{"role": "user", "content": prompt}],
                format="json",
                keep_alive="30m",
                options=options,
            )
        except Exception as e:
            print(f"  リクエスト{i}が失敗: {e}")
            break

        time.sleep(1.5)  # 解放処理を待つ
        snap = snapshot()
        rss, priv = totals(snap)
        sys_mb = system_committed_mb()

        print(f"{i:>3} {rss:>10,.0f} {rss - prev_rss:>+9,.0f} {priv:>11,.0f} "
              f"{priv - prev_priv:>+9,.0f} {sys_mb:>10,.0f} {sys_mb - prev_sys:>+9,.0f}")

        for (pid, name), v in snap.items():
            rows.append({
                "request": i, "pid": pid, "process": name,
                "rss_mb": round(v["rss_mb"], 1),
                "private_mb": round(v["private_mb"], 1),
                "system_used_mb": round(sys_mb, 1),
            })
        prev_rss, prev_priv, prev_sys = rss, priv, sys_mb

    if rows:
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)

        n = rows[-1]["request"]
        print("-" * 68)
        print(f"\n【結果】{n}リクエストでの増加:")
        print(f"  ollama系 RSS(ワーキングセット): {prev_rss - base_rss:+,.0f} MB "
              f"({(prev_rss - base_rss) / n:+,.0f} MB/件)")
        print(f"  ollama系 Private(コミット)    : {prev_priv - base_priv:+,.0f} MB "
              f"({(prev_priv - base_priv) / n:+,.0f} MB/件)")
        print(f"  システム全体使用量            : {prev_sys - base_sys:+,.0f} MB "
              f"({(prev_sys - base_sys) / n:+,.0f} MB/件)")
        print(f"\n  プロセス別内訳(最終時点):")
        for (pid, name), v in snapshot().items():
            b = base.get((pid, name))
            delta = f"{v['rss_mb'] - b['rss_mb']:+,.0f}" if b else "新規"
            print(f"    {name} (pid={pid}): RSS={v['rss_mb']:,.0f}MB (Δ{delta}MB)")
        print(f"\n  詳細を {args.csv} に保存しました。")


if __name__ == "__main__":
    main()