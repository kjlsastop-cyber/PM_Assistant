from __future__ import annotations

import argparse, csv, json, os, re, statistics, sys, time
from pathlib import Path
from dotenv import load_dotenv
from openai import OpenAI

ROOT = Path(__file__).resolve().parents[1]
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import kb

load_dotenv(ROOT / ".env")
SYSTEM = """你是严谨的项目助手。只依据提供的知识库和项目状态回答；资料不足时明确说无法确认。遇到新旧状态冲突，以日期较新的项目状态为准并说明变化。关键结论后用[来源文件名]标注来源。不要编造数字、日期或结论。"""
REVIEW = """审查回答是否遗漏标准答案、使用过时状态、编造事实或缺少必要来源。只输出JSON：{\"verdict\":\"pass|fail\",\"feedback\":\"具体修改意见\"}。"""
JUDGE = """你是评测裁判。比较候选答案与人工标准答案，按0-5评分：正确性、完整性、时效/冲突处理、来源与克制。只输出JSON：{\"score\":0到5,\"correct\":true或false,\"reason\":\"简述\"}。同义表达应判对；资料不足题若拒绝编造应判对。"""
USAGE={"prompt_tokens":0,"completion_tokens":0,"total_tokens":0}

def config(prefix=""):
    key=os.getenv(prefix+"OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY")
    base=os.getenv(prefix+"OPENAI_BASE_URL") or os.getenv("OPENAI_BASE_URL","https://api.openai.com/v1")
    model=os.getenv(prefix+"MODEL_NAME") or os.getenv("MODEL_NAME","gpt-4o-mini")
    if not key: raise SystemExit("缺少 OPENAI_API_KEY")
    return OpenAI(api_key=key, base_url=base), model

def reviewer_config(primary_model):
    """与 app.py 对齐：未显式指定时，DeepSeek 与通义千问错开互审。"""
    explicit=os.getenv("REVIEWER_MODEL","").strip()
    if explicit:
        key=os.getenv("REVIEWER_API_KEY") or os.getenv("OPENAI_API_KEY")
        base=os.getenv("REVIEWER_BASE_URL") or os.getenv("OPENAI_BASE_URL","https://api.openai.com/v1")
        return OpenAI(api_key=key,base_url=base),explicit
    q_key=os.getenv("EMBEDDING_API_KEY","").strip()
    q_model=os.getenv("QWEN_MODEL","").strip()
    if q_key and q_model and q_model != primary_model:
        return OpenAI(api_key=q_key,base_url=os.getenv("EMBEDDING_BASE_URL","https://dashscope.aliyuncs.com/compatible-mode/v1")),q_model
    return config()

def ask(client, model, messages, temperature=0):
    r=client.chat.completions.create(model=model,messages=messages,temperature=temperature)
    if getattr(r,"usage",None):
        for k in USAGE: USAGE[k]+=int(getattr(r.usage,k,0) or 0)
    return r.choices[0].message.content or ""

def parse_json(text):
    m=re.search(r"\{.*\}",text,re.S)
    return json.loads(m.group(0)) if m else {}

def memory_text(data):
    lines=[f"截至 {data['as_of']}，项目：{data['project']['name']}"]
    for kind in ("tasks","decisions","risks","events"):
        for x in data[kind]: lines.append(f"- {kind}: "+"；".join(f"{k}={v}" for k,v in x.items()))
    return "\n".join(lines)

def build_index(rebuild=False):
    store=ROOT/"kb_store___eval_controlled__.json"
    if rebuild and store.exists(): store.unlink()
    base=kb.KnowledgeBase("__eval_controlled__")
    docs=json.loads((HERE/"corpus.json").read_text(encoding="utf-8"))
    known=set(base.doc_names())
    for d in docs:
        if d["source"] not in known: base.add_document(d["source"],d["text"])
    return base

def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--limit",type=int); ap.add_argument("--rebuild-index",action="store_true"); ap.add_argument("--resume",action="store_true"); a=ap.parse_args()
    client,model=config(); judge_client,judge_model=config("JUDGE_")
    reviewer_client,reviewer_model=reviewer_config(model)
    cases=[json.loads(x) for x in (HERE/"cases.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    if a.limit: cases=cases[:a.limit]
    memory=memory_text(json.loads((HERE/"project_memory.json").read_text(encoding="utf-8")))
    index=build_index(a.rebuild_index)
    out=HERE/"results"; out.mkdir(exist_ok=True); raw=out/"raw.jsonl"
    done=set()
    if a.resume and raw.exists():
        done={(x["case_id"],x["arm"]) for x in map(json.loads,raw.read_text(encoding="utf-8").splitlines())}
    elif raw.exists():
        raw.unlink()  # 正式重跑时不混入旧冒烟数据
    arms=[("rag",False,False),("rag_reviewer",False,True),("rag_pm",True,False),("rag_pm_reviewer",True,True)]
    for case in cases:
        hits=index.search(case["question"])
        rag="\n\n".join(f"[{h['doc']}] {h['text']}" for h in hits) or "（未检索到资料）"
        for arm,use_pm,use_review in arms:
            if (case["id"],arm) in done: continue
            product_usage_before=USAGE.copy()
            context=f"【知识库】\n{rag}"+(f"\n\n【Project Memory】\n{memory}" if use_pm else "")
            msgs=[{"role":"system","content":SYSTEM},{"role":"user","content":context+"\n\n【问题】\n"+case["question"]}]
            product_t=time.time(); answer=ask(client,model,msgs); revised=False; review={}
            if use_review:
                # Reviewer 只能看到问题、实际上下文和候选答案，不能看到 gold，避免实验泄漏标准答案。
                review=parse_json(ask(reviewer_client,reviewer_model,[{"role":"system","content":REVIEW},{"role":"user","content":f"问题：{case['question']}\n可用上下文：{context}\n候选答案：{answer}"}]))
                if review.get("verdict")=="fail":
                    answer=ask(client,model,msgs+[{"role":"assistant","content":answer},{"role":"user","content":"请按审查意见修正："+review.get("feedback","")}]); revised=True
            product_latency=round(time.time()-product_t,2)
            product_usage={k:USAGE[k]-product_usage_before[k] for k in USAGE}
            judge_usage_before=USAGE.copy(); judge_t=time.time()
            judge=parse_json(ask(judge_client,judge_model,[{"role":"system","content":JUDGE},{"role":"user","content":f"问题：{case['question']}\n标准答案：{case['ideal_answer']}\n必含事实：{case['required']}\n禁止错误：{case['forbidden']}\n候选答案：{answer}"}]))
            judge_latency=round(time.time()-judge_t,2)
            judge_usage={k:USAGE[k]-judge_usage_before[k] for k in USAGE}
            rec={"case_id":case["id"],"category":case["category"],"arm":arm,"question":case["question"],"answer":answer,"ideal_answer":case["ideal_answer"],"hits":[h["doc"] for h in hits],"review":review,"revised":revised,"judge":judge,"product_latency_s":product_latency,"judge_latency_s":judge_latency,"product_usage":product_usage,"judge_usage":judge_usage}
            with raw.open("a",encoding="utf-8") as f: f.write(json.dumps(rec,ensure_ascii=False)+"\n")
            print(case["id"],arm,judge.get("score"))
    rows=[json.loads(x) for x in raw.read_text(encoding="utf-8").splitlines()]
    summary=[]
    categories=["all"]+sorted({c["category"] for c in cases})
    for arm,_,_ in arms:
      for category in categories:
        rs=[r for r in rows if r["arm"]==arm and r["case_id"] in {c["id"] for c in cases} and (category=="all" or r["category"]==category)]
        if not rs: continue
        summary.append({"arm":arm,"category":category,"n":len(rs),"avg_score":round(statistics.mean(float(r["judge"].get("score",0)) for r in rs),3),"accuracy":round(sum(bool(r["judge"].get("correct")) for r in rs)/len(rs),3),"avg_product_latency_s":round(statistics.mean(r["product_latency_s"] for r in rs),2),"avg_product_tokens":round(statistics.mean(r["product_usage"].get("total_tokens",0) for r in rs),1),"avg_judge_latency_s":round(statistics.mean(r["judge_latency_s"] for r in rs),2),"avg_judge_tokens":round(statistics.mean(r["judge_usage"].get("total_tokens",0) for r in rs),1),"revision_rate":round(sum(r["revised"] for r in rs)/len(rs),3)})
    (out/"summary.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding="utf-8")
    with (out/"summary.csv").open("w",newline="",encoding="utf-8-sig") as f:
        w=csv.DictWriter(f,fieldnames=summary[0]); w.writeheader(); w.writerows(summary)
    print(json.dumps(summary,ensure_ascii=False,indent=2))
if __name__=="__main__": main()
