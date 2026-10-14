# Accounts and access (no secrets in this file, ever)

| Thing | Where it lives | What the new bot should do |
|---|---|---|
| **qoresence@mail.grokbot.com** | A Grok Bot inbox owned by the **original** Grok Bot. Used for the Cerebral Valley application, Stripe and X | You probably **can't read it**. Ask Con to re-share access, or claim your own inbox (only with his OK on the name) |
| **GitHub** ConWan30/qortime, ConWan30/qorcap | Con's account. The original bot used a **fine-grained token limited to selected repos** (env `GH_QORESENCE_WORKFLOW_TOKEN` on the old box) | Con must grant **your** access: a new fine-grained token with Contents, Pull requests and Workflows read/write on both repos. Use it through `http.extraheader` and never print it |
| **Stripe** (test mode, `acct_1UQVjXCatAPuUvUA`) | Login stored **only** on the original bot's box at `/home/box/.secrets/stripe.env` | **Get it from Con** directly. Never put it in a repo or chat |
| **X** @QorTimeHQ (and reserved @Qortroller) | Con's X account, reachable through an X connector | Post only the approved queue, or with Con's explicit OK per post |
| **Cerebral Valley** (Meta hackathon) | Signed up with qoresence@mail.grokbot.com, handle conwanzo | Approval email lands in that inbox. Ask Con |
| **Laptop "MyTurn"** | Con's Windows laptop with the capture card and DualSense Edge | Reach it through registered-machine tools if Con connects it. Otherwise give him the steps |

Rules: never print or commit passwords or tokens. Never send email or messages as Con without his OK. Never deal with billing or KYC on your own.
