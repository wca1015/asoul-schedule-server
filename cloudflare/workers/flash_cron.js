/**
 * 突击直播定时触发器（Cloudflare Workers Cron）。
 *
 * 每 5 分钟触发一次，通过 GitHub API 触发 flash_monitor workflow。
 * 相比 GitHub 原生 cron（有排队延迟），Cloudflare Cron 是秒级准点触发，
 * 可将突击直播的端到端延迟压缩到 5 分钟左右。
 *
 * 部署步骤：
 *   1. wrangler init（或在 dashboard 创建 Worker）
 *   2. 粘贴本文件内容，wrangler.toml 中配置 cron trigger:
 *        [triggers]
 *        crons = ["*\/5 * * * *"]
 *   3. 设置 secrets: wrangler secret put GITHUB_TOKEN / GITHUB_REPO
 *
 * 环境变量：
 *   GITHUB_TOKEN — 具有 repo 权限的 PAT / Fine-grained token
 *   GITHUB_REPO  — "owner/repo" 格式
 */
export default {
  async scheduled(event, env, ctx) {
    const { GITHUB_TOKEN, GITHUB_REPO } = env;
    if (!GITHUB_TOKEN || !GITHUB_REPO) {
      console.error("缺少 GITHUB_TOKEN 或 GITHUB_REPO 配置");
      return;
    }

    const url = `https://api.github.com/repos/${GITHUB_REPO}/actions/workflows/flash_monitor.yml/dispatches`;

    const resp = await fetch(url, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${GITHUB_TOKEN}`,
        Accept: "application/vnd.github+json",
        "Content-Type": "application/json",
        "User-Agent": "asoul-flash-cron",
      },
      body: JSON.stringify({ ref: "main" }),
    });

    // GitHub 对成功的 dispatch 返回 204 No Content
    if (resp.status === 204) {
      console.log("flash_monitor workflow 触发成功");
    } else {
      console.error("触发失败:", resp.status, await resp.text());
    }
  },
};
