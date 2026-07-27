"""Ollama のホストメモリリークを定量化する計測スクリプト(v2)。

v1 からの変更点:
  - Ollama が返す prompt_eval_count / eval_count を記録し、
    「prefill トークン 1 個あたり何 MB 漏れるか」を厳密に算出する
  - 本文長を変えた複数条件を1回の実行でまとめて回し、
    最小二乗法で 傾き(MB/token) と 切片(1リクエスト固定コスト) を分離する

使い方:
    python memcheck2.py --model gemma4:12b-it-qat
    python memcheck2.py --model qwen3:8b
    python memcheck2.py --model gemma4:12b-it-qat --note vulkan   # バックエンド比較用
"""
import argparse
import csv
import random
import string
import time

import ollama
import psutil

TARGET_HINTS = ("ollama", "llama-server", "llama_server")

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


def runner_mem_mb():
    """llama-server 系プロセスの RSS / Private を MB で返す。"""
    rss = priv = 0.0
    for p in psutil.process_iter(["name"]):
        try:
            if any(h in (p.info["name"] or "").lower() for h in TARGET_HINTS):
                mi = p.memory_info()
                rss += mi.rss / 1024 ** 2
                priv += getattr(mi, "private", mi.rss) / 1024 ** 2
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return rss, priv


def make_prompt(body_len: int) -> str:
    rnd = "".join(random.choices(string.ascii_lowercase, k=8))
    body = (rnd * (body_len // 8 + 1))[:body_len]
    return BASE_PROMPT.format(sender=f"{rnd}@example.com", subject=f"件名{rnd}", body=body)


def linfit(xs, ys):
    """最小二乗法で y = a*x + b の a, b と決定係数 R^2 を返す。"""
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return 0.0, my, 0.0
    a = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    b = my - a * mx
    ss_res = sum((y - (a * x + b)) ** 2 for x, y in zip(xs, ys))
    ss_tot = sum((y - my) ** 2 for y in ys)
    r2 = 1 - ss_res / ss_tot if ss_tot else 1.0
    return a, b, r2


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--lengths", default="100,400,1500,4000", help="試す本文文字数(カンマ区切り)")
    ap.add_argument("--reps", type=int, default=4, help="各条件での繰り返し回数")
    ap.add_argument("--num-ctx", type=int, default=4096)
    ap.add_argument("--note", default="", help="CSVに残すメモ(backend名など)")
    ap.add_argument("--csv", default=None)
    args = ap.parse_args()

    lengths = [int(x) for x in args.lengths.split(",")]
    options = {"num_ctx": args.num_ctx, "num_predict": 64, "temperature": 0}
    csv_path = args.csv or f"memcheck2_{args.model.replace(':', '_').replace('/', '_')}.csv"

    print(f"model={args.model} note={args.note or '-'} lengths={lengths} reps={args.reps}")
    print("モデルをロード中...")
    ollama.chat(model=args.model, messages=[{"role": "user", "content": "ping"}],
                keep_alive="30m", options=options)
    time.sleep(3.0)

    rows = []
    print(f"\n{'本文字数':>8} {'回':>3} {'prefill tok':>12} {'gen tok':>8} "
          f"{'ΔRSS(MB)':>10} {'MB/tok':>8}")
    print("-" * 56)

    for blen in lengths:
        for r in range(1, args.reps + 1):
            before_rss, _ = runner_mem_mb()
            try:
                resp = ollama.chat(
                    model=args.model,
                    messages=[{"role": "user", "content": make_prompt(blen)}],
                    format="json", keep_alive="30m", options=options,
                )
            except Exception as e:
                print(f"  失敗: {e}")
                return
            time.sleep(1.5)
            after_rss, after_priv = runner_mem_mb()

            ptok = resp.get("prompt_eval_count", 0)
            gtok = resp.get("eval_count", 0)
            d = after_rss - before_rss
            per = d / ptok if ptok else 0.0
            print(f"{blen:>8} {r:>3} {ptok:>12} {gtok:>8} {d:>10,.1f} {per:>8.3f}")
            rows.append({
                "model": args.model, "note": args.note, "body_len": blen, "rep": r,
                "prompt_tokens": ptok, "gen_tokens": gtok,
                "delta_rss_mb": round(d, 2), "rss_after_mb": round(after_rss, 1),
                "priv_after_mb": round(after_priv, 1),
            })

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    # 最初の1回は初期化コストが混じるので各条件の2回目以降で回帰する
    fit = [r for r in rows if r["rep"] > 1 and r["prompt_tokens"] > 0]
    if len(fit) >= 3:
        a, b, r2 = linfit([r["prompt_tokens"] for r in fit],
                          [r["delta_rss_mb"] for r in fit])
        print("-" * 56)
        print(f"\n【回帰結果】 増加量(MB) = {a:.4f} x prefillトークン数 + {b:.1f}")
        print(f"  prefillトークンあたり : {a * 1024:,.0f} KB/token")
        print(f"  リクエスト固定コスト  : {b:,.1f} MB")
        print(f"  決定係数 R^2          : {r2:.4f}")
        if r2 > 0.9 and a > 0:
            print("  → トークン数に比例。切片がほぼ0ならリークは完全に prefill 由来")
        gen = sum(r["gen_tokens"] for r in fit) / len(fit)
        print(f"  (生成トークン数は平均 {gen:.0f}。条件間でほぼ一定なら生成の寄与は切片に入る)")
    print(f"\n{csv_path} に保存しました。")


if __name__ == "__main__":
    main()