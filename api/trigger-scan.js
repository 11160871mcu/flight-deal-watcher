// /api/trigger-scan.js
//
// 這支 API 本身完全不爬蟲、不打 Google Flights。它唯一做的事，
// 就是呼叫 GitHub 的 REST API，觸發你原本就有的
// .github/workflows/check-flights.yml（workflow_dispatch）。
//
// 真正的爬蟲工作 100% 還是跑在 GitHub Actions 上，這支 API
// 只是「按下開始按鈕」的角色。Vercel 的排程系統（crons）會定時
// 打這支 API，等於幫 GitHub Actions 自己的排程多加一條獨立的
// 觸發管道，不會受 GitHub Actions 整點附近容易延遲/被跳過的
// 問題影響。兩邊排程有可能重疊，但 workflow 裡已經設定
// concurrency 群組鎖，就算同時被觸發兩次也不會互相干擾。

export default async function handler(req, res) {

  // 如果有設定 CRON_SECRET 環境變數，Vercel Cron 呼叫這支 API
  // 時會自動在 Authorization header 帶上這個值，這裡驗證可以
  // 避免任何人隨便打這支網址亂觸發爬蟲。
  const cronSecret = process.env.CRON_SECRET;

  if (cronSecret) {
    const authHeader = req.headers.authorization || "";
    if (authHeader !== `Bearer ${cronSecret}`) {
      res.status(401).json({ ok: false, error: "unauthorized" });
      return;
    }
  }

  const token = process.env.GH_TRIGGER_TOKEN;
  const owner = process.env.GH_OWNER;
  const repo = process.env.GH_REPO;
  const workflowFile = process.env.GH_WORKFLOW_FILE || "check-flights.yml";
  const ref = process.env.GH_REF || "main";

  if (!token || !owner || !repo) {
    res.status(500).json({
      ok: false,
      error:
        "缺少環境變數，請到 Vercel 專案設定 GH_TRIGGER_TOKEN / GH_OWNER / GH_REPO",
    });
    return;
  }

  const url = `https://api.github.com/repos/${owner}/${repo}/actions/workflows/${workflowFile}/dispatches`;

  try {
    const ghResponse = await fetch(url, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${token}`,
        Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ ref }),
    });

    // GitHub 觸發 workflow_dispatch 成功時回傳 204 No Content。
    if (ghResponse.status === 204) {
      res.status(200).json({
        ok: true,
        message: "已成功觸發 GitHub Actions（check-flights.yml）",
        triggeredAt: new Date().toISOString(),
      });
      return;
    }

    const detail = await ghResponse.text();
    res.status(502).json({
      ok: false,
      error: "GitHub API 回應非預期狀態碼",
      status: ghResponse.status,
      detail,
    });
  } catch (err) {
    res.status(500).json({ ok: false, error: String(err) });
  }
}