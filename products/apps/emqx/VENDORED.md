# EMQX source and adaptation provenance

The starting implementation is WOOWTECH's MIT-licensed
`Woow_emqx_mcp_server@1be17bad5aef6c7b7519686ccbfe1d80762bc10e`, taken from
`Woow_ha_mcp_addons@28b8317ec12c47c81c946fe9aaec18dd1d5ad852` under
`apps/emqx/vendor/emqx_mcp_server/`. The original copyright/license is preserved
in [LICENSE](LICENSE). No production credentials or customer data are included.

[source-manifest.json](docs/source-manifest.json) records each starting HA file's
SHA-256 and the adapted package SHA-256. This is a source manifest, not an OCI image
attestation. Original PaaS base: `7f0eb78f6edec39166b74d88a53307c4614dcf35`;
current integration base: merged PR95 `3c93d0e727fdc948f1ea17cb5fa7d1ad1887a883`.

This package is an explicitly adapted copy, **not byte-identical vendor source**:

- `http_policy.py` is the app-owned HTTP transport. Its reviewed HTTPX/httpcore
  pool/address-pinning technique and dependency source hashes follow the HA runtime
  transport; actual destination rules come from PaaS core SSRF policy. There is no
  `backend_policy` shim or permissive transport substitution.
- `lifespan.py` owns that real protected client and closes its pool on shutdown.
- `server.py` registers the complete surface and adds live saved-policy middleware.
  Environment registration gates are not the PaaS authorization source.
- `settings.py` removes dotenv and hidden localhost destination fallback.
- `errors.py` supplies bounded generic HTTP failures and rejects malformed/empty
  HTTP 200 JSON. Explicit 204 remains valid for no-content responses.
- `tools/integration.py` marks SQL testing as write, matching HA's exact grant
  semantics. The grant is enabled by the all-on default, not default-off.
- `tools/messaging.py` propagates retained lookup failures except 404 and removes
  the dangerous publish fallback after failed deletes. Regression tests first
  reproduced two requests on a 503; the adapted behavior makes one request.
- `tools/security.py` avoids reflecting broker-provided source names in errors.

The new admin package, tests, app-specific frontend, Docker recipe and smoke script
are PaaS integration work. Tests exercise the pinned APIs with installed Python
3.12.14, FastMCP 4.1.3, MCP 2.3.0, HTTPX 0.28.1 and httpcore 1.0.9. Root locks remain
main's responsibility. Do not update the HTTP dependencies or copy an HA stub to
bypass source-hash failures; review the changed transport and rerun socket tests.

## PR95 之後在這個副本上的改寫

下面幾項是 PR95 之後才改的（這個副本本來就不是 byte-identical 的 vendor）。路徑段規則與 `%` 規則的修正
（`tools/_common.py`、`http_policy.py`）當時沒有更新本檔；2026-11-15 的 EMQX 實測小修一併補在這裡：

- `http_policy.py`：`path_value` 的路徑段與 `%` 規則（與 HA add-on 同一套）；`BrokerClient.request` 多一個固定集合的
  關鍵字 `accept`（預設 `application/json`，只有 `emqx_prometheus_stats` 用 `text/plain`；其他值、`headers` 與其他請求選項
  一律照舊拒絕）；httpx 組不出來的網址（超過 65,536 字元，或值含單獨的 surrogate 而無法編碼）在送出前以
  `DestinationDenied`（`EMQX_DESTINATION_DENIED`）拒絕。傳輸層的來源雜湊檢查與相依套件版本沒有動。
- `errors.py`：新增 `text_body`（`json_body` 的文字版：空內容不算成功）。
- `tools/_common.py`：`path_segment` 拒收空字串、`.`、`..`，並對無法編碼的值回 `EMQX_DESTINATION_DENIED`。
- `tools/cluster.py`：`emqx_prometheus_stats` 送 `Accept: text/plain`，才會拿到說明所寫的 Prometheus 文字
  （EMQX 對 JSON 的 Accept 回的是 JSON 統計）。
- `tools/topics.py`：`emqx_list_subscriptions` 的工具說明與 `match_topic` 參數說明改成 EMQX 5.8.9 實測的語意
  （你給的字串當過濾器、去比對已儲存的訂閱主題；用具體主題查不到萬用字元訂閱）。

這些改動的測試在 `tests/test_subscription_match_topic.py`、`tests/test_prometheus_accept.py`、
`tests/test_long_identifiers.py`。`docs/source-manifest.json` 的 `adapted_sha256` 與 `changed_from_ha` 在 2026-11-15 依目前
檔案重算（先前的修正沒有更新它，重算前有 8 個檔案與記錄不符）；沒有任何測試或腳本比對它。
`docs/validation-source.json` 是 PR95 當時的來源階段驗證紀錄，沒有改。
