/**
 * B 站 API 反代（Cloudflare Worker）。
 *
 * 目的：把 GitHub Actions 的抓取请求改道到本 Worker，用 Cloudflare 的
 * 出口 IP 访问 B 站，规避 Actions 共享数据中心 IP 触发的高频风控（412）。
 *
 * 行为：透明转发。请求路径与查询串原样透传给上游，请求头（UA / Referer /
 * Cookie 等）原样保留；上游主机由请求头 X-Bili-Upstream 指定，缺省
 * api.bilibili.com（白名单校验，防止被当开放代理滥用）。
 *
 * 部署步骤：
 *   1. 在 Cloudflare Dashboard 创建 Worker（或 `wrangler init`）
 *   2. 粘贴本文件内容并部署
 *   3. （可选）`wrangler secret put BILI_PROXY_KEY` 设置共享密钥
 *   4. 在 GitHub Actions Secrets 配置：
 *        BILI_PROXY_URL=https://<你的worker域名>.workers.dev
 *        若设置了 BILI_PROXY_KEY，同时配置 BILI_PROXY_KEY
 *
 * 安全提示：上游白名单限制 + 可选共享密钥，避免 Worker 被第三方当免费代理。
 */
const ALLOWED_UPSTREAMS = new Set([
  "api.bilibili.com",
  "api.live.bilibili.com",
  "api.vc.bilibili.com",
]);

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    // 可选共享密钥：环境变量 BILI_PROXY_KEY（Python 侧通过 X-Bili-Key 头携带）
    if (env.BILI_PROXY_KEY) {
      const auth = request.headers.get("X-Bili-Key");
      if (auth !== env.BILI_PROXY_KEY) {
        return new Response("forbidden", { status: 403 });
      }
    }

    let upstream = (request.headers.get("X-Bili-Upstream") || "api.bilibili.com").toLowerCase();
    if (!ALLOWED_UPSTREAMS.has(upstream)) {
      return new Response("forbidden upstream", { status: 403 });
    }

    // 目标 URL 由上游主机 + 原路径/查询串拼出（Host 由 URL 自动确定）
    const target = `https://${upstream}${url.pathname}${url.search}`;
    const headers = new Headers(request.headers);
    headers.delete("X-Bili-Upstream");
    headers.delete("X-Bili-Key");

    const init = { method: request.method, headers, redirect: "follow" };
    if (request.method !== "GET" && request.method !== "HEAD") {
      init.body = request.body;
    }

    try {
      const resp = await fetch(target, init);
      const outHeaders = new Headers(resp.headers);
      outHeaders.set("Access-Control-Allow-Origin", "*");
      return new Response(resp.body, {
        status: resp.status,
        statusText: resp.statusText,
        headers: outHeaders,
      });
    } catch (err) {
      return new Response(`upstream error: ${err.message}`, { status: 502 });
    }
  },
};
