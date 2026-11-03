#!/bin/bash
# 给一张图生成描述 + 情绪/场合标签 + 是否表情包。输出一行 JSON。
# 用法: ./desc-gpt.sh /path/to/img
#
# 需要两个环境变量：
#   MEME_LLM_URL   完整端点，例如 https://api.openai.com/v1/chat/completions
#   MEME_LLM_KEY   你的 API key
#   MEME_LLM_MODEL 可选，默认 gpt-4o-mini
#
# 任何 OpenAI 兼容的视觉端点都能用，不一定非得是 OpenAI。
set -e
: "${MEME_LLM_URL:?需要设置 MEME_LLM_URL}"
: "${MEME_LLM_KEY:?需要设置 MEME_LLM_KEY}"
MODEL="${MEME_LLM_MODEL:-gpt-4o-mini}"

IMG="$1"
[ -f "$IMG" ] || { echo '{"desc":"（读不到这张图）","mood":["中性"],"scene":["闲聊"],"is_meme":false}'; exit 0; }
TMP=$(mktemp /tmp/memedesc.XXXXXX.json); trap 'rm -f "$TMP"' EXIT
python3 - "$IMG" "$TMP" <<'PY'
import base64, json, sys
img, out = sys.argv[1], sys.argv[2]
ext = img.rsplit(".",1)[-1].lower()
mime = {"jpg":"image/jpeg","jpeg":"image/jpeg","png":"image/png","gif":"image/gif","webp":"image/webp"}.get(ext,"image/png")
# 送模型前压到 512。原图直送一张手机截图就上万视觉 token，
# 而这个任务只需要看清表情和字，512 够了。
raw = open(img, "rb").read()
try:
    from PIL import Image
    import io
    im = Image.open(io.BytesIO(raw)).convert("RGB")
    im.thumbnail((512, 512))
    buf = io.BytesIO(); im.save(buf, "JPEG", quality=85)
    raw, mime = buf.getvalue(), "image/jpeg"
except Exception:
    pass  # 没装 Pillow 就送原图，只是费点钱
b64 = base64.b64encode(raw).decode()
prompt = (
 "判断这张图是不是聊天用的表情包（单个人物/动物/梗图，带夸张表情或文字，用于表达情绪）。"
 "截图、网页、聊天记录、风景、文档、立绘原图都不算表情包。\n\n"
 "然后：1) 把图上所有文字原样读出来，没字写「无字」；2) 描述画面里什么角色、什么表情动作；"
 "3) 判断适合什么场合发。\n\n"
 "严格只输出 JSON：\n"
 '{"is_meme": true或false, "text": "图上的字，原样抄", '
 '"desc": "40到80字中文描述，要包含图上的字的意思", '
 '"mood": ["从这8个选1到2个：得意/委屈/嘴硬/开心/呆/摸鱼/凶/撒娇"], '
 '"scene": ["从这5个选1个：被夸/被怼/干饭/干活/闲聊"]}'
)
req = {"model":"__MODEL__","messages":[{"role":"user","content":[
  {"type":"text","text":prompt},
  {"type":"image_url","image_url":{"url":f"data:{mime};base64,{b64}"}}]}],
  "max_tokens":400,"temperature":0.2}
json.dump(req, open(out,"w"), ensure_ascii=False)
PY
# 模型名从环境变量塞进去，避免在脚本里写死任何一家
sed -i "s/__MODEL__/$MODEL/" "$TMP"

RESP=$(curl -s -m 90 "$MEME_LLM_URL" \
  -H "Authorization: Bearer $MEME_LLM_KEY" \
  -H "Content-Type: application/json" --data-binary "@$TMP")
echo "$RESP" | python3 -c "
import json,sys,re
try:
    d=json.load(sys.stdin); t=d['choices'][0]['message']['content']
    m=re.search(r'\{.*\}', t, re.S); o=json.loads(m.group(0)) if m else {}
    print(json.dumps({'desc':str(o.get('desc','（没读出内容）'))[:100],
                      'mood':o.get('mood') or ['中性'],
                      'scene':o.get('scene') or ['闲聊'],
                      'is_meme':bool(o.get('is_meme', False))},ensure_ascii=False))
except Exception as e:
    print(json.dumps({'desc':'（生成失败）','mood':['中性'],'scene':['闲聊'],'is_meme':False},ensure_ascii=False))
"
