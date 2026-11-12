# CONTRACTS：woow_document_management 跨 lane 介面契約（公開版）

本文是內部設計包裡「跨 lane 介面契約」和「代號表」的淨化版，給實作和審查這個 repo 的人使用：
拿掉了站名、人名、筆數和內部路徑；只在 repo 外執行的一次性重建腳本寫成「L10 site-rebuild（repo 外）」，
它的介面只保留通則（§14）。

- 位階：設計勘誤（ER-xx）＞架構＞資安與 sweep 設計＞產品規格＞測試計畫＞本文。衝突時以 ER 為準。
- `docs/CONTRACTS.md` 只有 L0 ci-sweep 能改：其他 lane 在 PR 描述的 `### CONTRACTS` 區塊交出變更紀錄文字，由 L0 合併（§21 B5）。
- 第 2 部分（代號表）收錄代號表 §1、§2.1～2.2、§4、§5；第 3 部分是變更紀錄。
- 文中的 core 行號以 Odoo 18 Community core 原始碼為準。

---

# 第 1 部分：跨 lane 介面契約


## 0. 讀法

- **IF-xxx**：一個跨 lane 介面。每節都寫明以下幾項：提供者（provider）、使用者（consumers）、模組路徑、精確簽名、例外和回傳行為、交易與鎖、su 行為、常數、L0 第 0 天的 stub。
- 簽名用 Python 3.10 型別註記。`Recordset[...]` 指 Odoo recordset。`Response` 指 `odoo.http.Response`。
- 「W/」＝`woow_document_management/`。
- 本文的章節號碼沿用 lanes.json 引用的位置：§1.3 檔案表、§2.2 manifest、§3 stub 表、§4.9 public method、§9 IF-STREAM、§10 IF-SHARE、§17 IF-TEST。設計勘誤 ER-43 引用的是舊 ic 的章節號碼，換算見 §20。
- 本文自己做的決議編成 **IC-Rnn**，和 PR-xx、ER-xx 分開。

---

## 1. 通則（每條 lane 都要遵守）

### 1.1 擁有權與變更
1. 一個檔案只有一個擁有 lane（§1.3）。L0 第 0 天寫進 stub，**不算擁有權**（ER-32 第 2 點）。之後只有擁有的 lane 能修改。
2. 要改本文的任何簽名、xmlid、常數或設定鍵，都要對擁有的 lane 發「契約變更請求」。擁有 lane 合併時同步更新 `docs/CONTRACTS.md` 的變更紀錄（由 L0 落實）。
3. `depends_on_interfaces` 只代表合併順序（ER-32 第 3 點）。L0 合併後，L1～L8 和 L11 就可以對著 stub 開工。L10 要等 L3 合併，才能在沙盒跑。
4. 合併順序：L0 → L1 → L2 → L3 → L4 → L5／L6／L7（可以並行）→ L8 → L10 → L9 → L11 收尾 → L12（IDS §1.3）。

### 1.2 共通執行規則

**R-RESP：拒絕和限流一律 return Response，不准 raise（ER-29）**
- `controllers/main.py`、`portal.py`、`share.py` 的每一條失敗路徑，都要 `return` IF-STREAM 的 `uniform_404(kind)`、`too_many_requests()`、`service_busy()`、`json_response(...)`，或 `limit_page(...)` 其中之一。
- 路由裡的 `AccessError`、`MissingError`、`UserError`、`ValidationError`、`WoowUploadRejected`、`LockBusy`、`ZipLimitExceeded` 都在 controller 接住，再 return。
- 禁止寫 `raise request.not_found(`、`return request.not_found(`、`raise NotFound`、`raise werkzeug.exceptions.`、`abort(`（T-STAT-23）。理由：`request.not_found()` 回傳的是 werkzeug 的 `NotFound` 例外物件（`http.py` L1892）；而 raise 會讓整個請求 rollback（`service/model.py` L163），S1／Q1 寫下的拒絕紀錄和限流計數會跟著消失。
- model 層照常 raise（AccessError、UserError 等）。只有 controller 有「轉成 Response」的義務。
- 失敗路徑上唯一允許、而且會 commit 的寫入是 S1／Q1（I-11、ER-29 第 4 點）。其他寫入都必須在已經 rollback 的 savepoint 裡。

**R-TX：交易**
- **上傳**：一批檔案共用一個 `with env.cr.savepoint():`，在裡面呼叫 `Document.create([...])`。任何一個檔案失敗，savepoint 就 rollback，然後 return 400 JSON。遇到 `LockBusy` 則 return `service_busy()`（503）。
- **ZIP**：在請求的交易裡建到 `SpooledTemporaryFile`，鎖會一直持有到 commit。串流在 commit 之後才開始（`wrap_file` 加 `call_on_close`）。
- **分享下載**：`lock('share:<id>')` 之後依序做三件事：在建立者 env 計算 `share_download` 的 ok 筆數、比對 `max_downloads`、用 S1 記錄。這三件事完成之後才 return 串流回應。串流在 commit 之後進行。
- **cron**：每一筆記錄包一個 savepoint，失敗的就跳過（R2b-06）。
- **ER-06 重建腳本**：單一交易，commit 之前先驗證（§14）。

**R-LOCK：鎖（ER-24、ER-47.3）**
- 所有 advisory lock 一律經由 `W/services/locks.py`（IF-LOCK）。其他檔案出現 `pg_advisory`、`pg_try_advisory` 就失敗（T-STAT-22）。
- 取鎖順序：`zip`（rank 1）→ `share`（2）→ `quota`（3）。同一個交易裡先拿了高 rank、又要拿低 rank → `AssertionError`。同 rank 重入可以。
- 鎖都是 transaction 級（`*_xact_lock`），commit 或 rollback 時自動釋放。

**R-SU：su 的行為（ER-45.3、ER-45.4、CA-33）**

| 位置 | `env.su` 為真時 | 非 su 時 |
|---|---|---|
| guard mixin 在 create 補做欄位 groups 檢查 | 略過 | 檢查，沒權限 → AccessError |
| folder.create 的根資料夾政策（M-24） | 略過 | `not parent_id and not 文件管理員 and not allow_root_create` → AccessError |
| ir.attachment 守衛 | 略過（框架的 `Binary` 欄位操作是 sudo，CA-07） | 檢查 |
| share 的寫入白名單、sentinel | 略過 | 檢查 |
| document.unlink | 允許（cron、hook） | 只有文件管理員，而且不能有 legal_hold |
| `_woow_has_rank`、`_woow_require_rank` | 一律成立 | 依 rule |
| `_cron_*`、`_cron_check_warn`、`_maintenance_run` | 必要條件 | 第一個陳述句就丟 AccessError |
| uninstall_hook | env 本來就是 su（CA-28） | — |
| mail_thread_guard | 看 `env.user._is_internal()`（OdooBot 是內部使用者） | 同左 |

`with_user()` 產生的 env 一律是 `su=False`（CA-33）。所以 S2-b 的建立者 env 和 ER-06 的驗證 env 都會套用 rule。

**R-LOG：log 和輸出衛生**
- `_logger` 只記 id。log 字串裡不准出現 token、password、email 或名稱類變數。
- 稽核 `detail` 只放整數、布林值和整數清單。

### 1.3 檔案擁有表（`W/`＝`woow_document_management/`）

| lane | 擁有的檔案 |
|---|---|
| L0 ci-sweep | `.gitignore`、`.gitattributes`、`LICENSE`、`COPYING`、`README.md`、`CLEANROOM.md`、`SECURITY.md`、`docs/CONTRACTS.md`、`.security/sweep.yaml`、`.github/workflows/security.yml`、`.github/gitleaks.toml`、`.github/pull_request_template.md`、`tools/**`（含 `tools/routes.lock.json`，IC-R12）、`W/__manifest__.py`、`W/__init__.py`、`W/{models,controllers,wizards,services}/__init__.py`、`W/security/groups.xml`（PR-01）、推送前檢查腳本與其字串清單（兩者都在 repo 外）、plat 的 target `Woow_odoo_document_management.yaml`、README target 表新增的那一列 |
| L1 settings-ip | `W/models/settings.py`、`W/models/res_config_settings.py`、`W/views/res_config_settings_views.xml`、`W/services/client_ip.py`、`W/tests/test_settings_bounds.py`、`W/tests/test_client_ip.py` |
| L2 upload-quota | `W/models/upload_policy.py`、`W/models/quota.py`、`W/services/naming.py`、`W/services/mime_map.py`、`W/services/locks.py`、`W/tests/test_upload_policy.py`、`test_naming.py`、`test_quota.py`、`test_locks.py`、`dev/sc/sc_quota_contention.py` |
| L3 core-models-acl | `W/models/guard_mixin.py`、`document_folder.py`、`folder_access.py`、`document.py`、`document_tag.py`、`document_share.py`、`document_log.py`、`ir_attachment.py`、`mail_thread_guard.py`、`W/hooks.py`、`W/services/constants.py`、`folding.py`、`url_policy.py`、`W/security/ir.model.access.csv`、`rules.xml`、`W/data/admin_assignment.xml`、`W/views/safe_url_templates.xml`、`W/tests/test_core_models.py`、`test_security_acl.py`、`test_multicompany.py`、`test_attachment_guard.py`、`test_mail_thread_guard.py`、`test_uninstall.py`、`test_display_name_folding.py`、`test_folding_service.py`、`test_url_policy.py`、`dev/sc/sc_uninstall.py` |
| L4 internal-routes-zip | `W/controllers/streaming.py`、`W/controllers/main.py`、`W/services/zip_builder.py`、`W/models/zip_actions.py`、`W/views/common_templates.xml`、`W/tests/test_zip_builder.py`、`test_internal_routes.py`、`test_native_routes.py` |
| L5 share | `W/models/share_service.py`、`W/services/tokens.py`、`W/wizards/share_wizard.py`、`W/views/wizards/share_wizard_views.xml`、`W/views/share_views.xml`、`W/views/share_templates.xml`、`W/controllers/share.py`、`W/static/src/share/**`、`W/static/src/share_result/**`、`W/static/tests/tours/share/**`、`W/data/mail_templates.xml`、`W/security/share_access.xml`、`W/tests/test_share_token.py`、`test_share_routes.py`、`test_mail_share.py`、`dev/sc/sc_share_max_downloads_race.py`、`dev/sc/sc_denied_count_real_http.py`、`dev/sc/sc_source_limit_two_links.py`、`dev/sc/sc_denied_sources_together.py`、`dev/sc/sc_denied_many_sources.py`、`dev/sc/sc_denied_one_source_together.py`、`dev/sc/sc_http_runtime.py` |
| L6 trash-audit-cron | `W/models/trash_actions.py`、`W/models/cron_jobs.py`、`W/wizards/restore_wizard.py`、`purge_wizard.py`、`W/views/wizards/restore_wizard_views.xml`、`purge_wizard_views.xml`、`W/security/trash_access.xml`、`W/data/ir_cron.xml`、`W/tests/test_trash_lifecycle.py`、`test_audit_log.py`、`test_cron.py`、`dev/sc/sc_purge_skip_locked.py` |
| L7 portal | `W/controllers/portal.py`、`W/views/portal_templates.xml`、`W/static/src/portal/**`、`W/tests/test_portal_routes.py`、`test_portal_upload.py` |
| L8 backend-ui | `W/views/backend/{folder,document,tag,trash,log,attachment_overview}_views.xml`、`W/views/menus.xml`、`W/views/wizards/{move,add_attachment,notify}_wizard_views.xml`、`W/wizards/{move,add_attachment,notify}_wizard.py`、`W/models/backend_actions.py`、`W/security/backend_access.xml`、`W/static/src/{backend,scss,img}/**`、`W/static/description/**`、`W/static/tests/tours/backend/**`、`W/tests/test_attachment_overview.py`、`test_add_attachment.py`、`test_backend_views.py`、`test_backend_wizards.py` |
| L9 i18n-zh-tw | `W/i18n/woow_document_management.pot`、`W/i18n/zh_TW.po`、`docs/i18n-glossary.md`、`W/tests/test_i18n_complete.py` |
| L10 site-rebuild（repo 外） | 重建腳本與其測試（repo 外，不在公開 repo；ER-45 第 1 點） |
| L11 test-suites | `W/tests/__init__.py`（第 0 天版本由 L0 寫）、`W/tests/common.py`、`W/tests/EXPECTED_MIN.json`、`test_record_rules.py`、`test_core_assumptions.py`、`test_rpc_exposure.py`、`test_escaping.py`、`test_perf.py`、`W/static/tests/tours/test_suites/**`、`dev/README.md`、`dev/sandbox_test.sh`、`dev/coverage.sh`、`dev/sc/run_sc.sh`、`dev/sc/sc_ca{06,23,24,28}.py` |
| L12 paas-release | `docs/release/v18.0.1.0.0.md`；repo 外的 `ops/release/**`、`ops/g-cache/**`、`ops/g-ip/**`、`ops/g-cookie/**`、`ops/residual-check/**`；cloud-odoo 和 plat 的打包與 lint 檔案（lanes L12） |

`dev/sc/` 底下每支腳本的擁有者以上表為準，這是「`dev/**` 歸 L11」的例外（IDS §1.4）。

### 1.4 交給 L0 的文字（README 段落、SECURITY.md 的 HD）
- HD 文字分工（IDS §6）：
  - L3：HD-02、03、04、07、09、10（ER-15 版）、12（ER-36）；
  - L5：HD-01、06、08（ER-35 版）、11（ER-47.7 定稿）；
  - L6：HD-05。
- 各 lane 在 PR 描述的 `### HD` 和 `### README` 區塊交出文字，由 L0 合併進 SECURITY.md 和 README.md。

### 1.5 UI 訊息 ID 的擁有者（文案依 ps §7，英文原文自己寫）

| 訊息 | 擁有者 | 備註 |
|---|---|---|
| M-01、M-02、M-20～M-24、M-30 | L3 | M-24：ER-05 的根資料夾政策；M-30：url_policy |
| M-10～M-16、M-19、M-57 | L2 | M-57 由 `_cron_check_warn` 產生 |
| M-17 | L7 | — |
| M-18、M-31 | L8 | 加入資料夾精靈、搬移精靈 |
| M-32～M-35 | L6 | — |
| M-40～M-45 | L4 | M-45 是 ER-24 新增的 |
| M-50～M-56、M-60～M-66 | L5 | M-61 改用 ER-12 的新文案 |
| M-70、M-71 | L3 | ER-30 |
| M-80、M-81 | 廢止 | ER-27 |

---

## 2. IF-SKEL：repo 骨架、manifest、匯入順序

- **提供者**：L0。
- **使用者**：L1～L9、L11、L12。L10 間接使用：它在沙盒安裝這個模組。

### 2.1 repo 結構
- 依 arch §1.2，扣掉 LU 和 MS 兩個模組（ER-02）。
- 只有一個 addon `woow_document_management`，另外有 `tools/`、`dev/`、`docs/`、`.security/`、`.github/`。
- `services/tests/` 這個目錄不建立（IDS §2 第 6 點）。

### 2.2 `W/__manifest__.py`（純字面量，給 `ast.literal_eval` 讀；T-STAT-13）
```python
{
    'name': 'Woow Document Management',
    'version': '18.0.1.0.0',
    'category': 'Productivity/Documents',
    'summary': 'Folders, permissions, portal and expiring share links for documents',
    'license': 'LGPL-3',
    'author': 'WoowTech',
    'website': 'https://github.com/WOOWTECH/Woow_odoo_document_management',
    'depends': ['mail', 'portal'],
    'data': [
        'security/groups.xml',
        'data/admin_assignment.xml',
        'security/ir.model.access.csv',
        'security/rules.xml',
        'security/share_access.xml',
        'security/trash_access.xml',
        'security/backend_access.xml',
        'data/ir_cron.xml',
        'data/mail_templates.xml',
        'views/safe_url_templates.xml',
        'views/common_templates.xml',
        'views/res_config_settings_views.xml',
        'views/backend/folder_views.xml',
        'views/backend/document_views.xml',
        'views/backend/tag_views.xml',
        'views/backend/trash_views.xml',
        'views/backend/log_views.xml',
        'views/backend/attachment_overview_views.xml',
        'views/share_views.xml',
        'views/wizards/share_wizard_views.xml',
        'views/wizards/move_wizard_views.xml',
        'views/wizards/restore_wizard_views.xml',
        'views/wizards/purge_wizard_views.xml',
        'views/wizards/add_attachment_wizard_views.xml',
        'views/wizards/notify_wizard_views.xml',
        'views/portal_templates.xml',
        'views/share_templates.xml',
        'views/menus.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'woow_document_management/static/src/backend/**/*',
            'woow_document_management/static/src/share_result/**/*',
            'woow_document_management/static/src/scss/**/*',
        ],
        'web.assets_frontend': [
            'woow_document_management/static/src/portal/**/*',
        ],
        'woow_document_management.assets_share': [
            'woow_document_management/static/src/share/**/*',
        ],
        'web.assets_tests': [
            'woow_document_management/static/tests/tours/**/*',
        ],
    },
    'application': True,
    'installable': True,
    'auto_install': False,
    'uninstall_hook': 'uninstall_hook',
}
```
- 不寫 `external_dependencies`、`post_init_hook` 和 `pre_init_hook`（ER-38）。
- data 的順序有兩個硬性規定：`data/admin_assignment.xml` 緊接在 `security/groups.xml` 之後（ER-38）；`views/menus.xml` 放最後，因為它要引用其他 lane 的 action。
- 分享落地頁只載入自己的 bundle `woow_document_management.assets_share`（landing.js 和最少量的 CSS），不載入整包 frontend，這樣才能滿足 T-SHARE-42（CSP 不允許 inline script）。

### 2.3 `__init__.py` 的匯入順序（L0 擁有；`tests/__init__.py` 由 L11 擁有）
- `W/__init__.py`：
  ```python
  from . import services, models, wizards, controllers
  from .hooks import uninstall_hook
  ```
- `services/__init__.py`：`constants, naming, mime_map, locks, tokens, url_policy, client_ip, folding, zip_builder`。
- `models/__init__.py`：`guard_mixin, settings, upload_policy, quota, document_tag, document_folder, folder_access, document, document_share, document_log, ir_attachment, ir_binary, mail_thread_guard, res_config_settings, share_service, trash_actions, cron_jobs, zip_actions, backend_actions`。
- `wizards/__init__.py`：`share_wizard, move_wizard, restore_wizard, purge_wizard, add_attachment_wizard, notify_wizard`。
- `controllers/__init__.py`：`streaming, main, portal, share`。
- `tests/__init__.py`（第 0 天版本）：匯入 ER-23 列出的 35 個測試檔，再加上 `test_perf`。

### 2.4 XML 命名規則
- 所有 xmlid 都不帶模組前綴寫在自己的檔案裡；跨 lane 引用時寫成 `woow_document_management.<id>`。
- QWeb 範本的 id 用 `snake_case`。

---

## 3. L0 第 0 天 stub（ER-32；每一個 stub 都標 `# WOOW-STUB`；控制存取的 stub 一律 fail closed）

- stub 的寫法是：完整簽名、docstring、`# WOOW-STUB`。stub 以外的地方寫 `raise NotImplementedError  # WOOW-STUB`，check_public_methods 在非 G1 模式視為合規。
- 所有持久、抽象和 transient model 都先宣告 `_name`、`_inherit` 和 arch §3 的欄位型別（含 groups、ondelete）。compute 一律是 fail-closed 的 stub，例如 eff_* 給空集合、rank 給 0。
- ACL CSV 第 0 天只有標頭，rules.xml 是 `<odoo/>`。所以在 L3 合併之前，只有 su 能操作本模組的 model。L1 和 L2 的測試要用 su 或 BaseCase。
- 到 G1 時，T-STAT-16 要求 `WOOW-STUB` 是 0 筆。

| 檔案（擁有者） | stub 行為 | 出處 |
|---|---|---|
| `models/settings.py`（L1） | `_get_settings()` 回傳 §6.1 預設值組成的 frozen `WoowDocSettings`；`_share_keys()` 丟 NotImplementedError；`_read_params()` 回傳 `{}` | ER-32 |
| `services/client_ip.py`（L1） | `client_ip()` 只回傳 peer（`environ['REMOTE_ADDR']`）；`request_client_ip()` 同上；`truncate_ip()` 回傳 `''`；`hmac_ip()` 和 `parse_trusted_proxies()` 分別丟 NotImplementedError 和回傳 `()` | IC-R01 |
| `models/res_config_settings.py`（L1） | 宣告欄位；`set_values()` 第一個陳述句是 `if not self.env.user.has_group('base.group_system'): raise AccessError`，接著丟 NotImplementedError | IC-R01 |
| `models/upload_policy.py`（L2） | `_validate` 丟 `WoowUploadRejected('stub')`；`_served_mimetype` 回傳 `'application/octet-stream'`；`_inline_allowed` 回傳 False | ER-32 |
| `models/quota.py`（L2） | `_check` 丟 `WoowUploadRejected('quota')`；`_usage_bytes` 和 `_bytes_if_new` 丟 NotImplementedError；`_cron_check_warn` 丟 AccessError | ER-32 |
| `services/locks.py`（L2） | `try_lock` 回傳 False；`lock` 丟 `LockBusy`；`bounded_wait` 是丟 `LockBusy` 的 context manager；`advisory_key` 用最終演算法（純函式）；`_reset_tracker` 不做事 | ER-32、IC-R09 |
| `services/naming.py`、`mime_map.py`（L2） | 常數用最終值；函式丟 NotImplementedError | IC-R01 |
| `services/constants.py`（L3） | **第 0 天就寫最終值**（§5）。常數不是 stub，不標 `WOOW-STUB` | IC-R02 |
| `models/document_log.py`（L3） | `_log_event` 不做事，回傳 0 | ER-32 |
| `models/document_folder.py`（L3） | `_woow_has_rank` 和 `_woow_portal_can_upload` 都回傳 False；`_woow_require_rank` 在非 su 時丟 AccessError；`_woow_rank_domain` 回傳 `[(0, '=', 1)]`；`woow_user_rank` 是 0；`woow_has_portal_members` 是 False；`_woow_breadcrumb` 回傳 `[(None, ELLIPSIS)]`；`_woow_portal_roots` 和 `_woow_portal_children` 回傳空 recordset | ER-32 |
| `models/guard_mixin.py`（L3） | `create` 在非 su 時只要 vals 有任何欄位就丟 AccessError；`_require_manager` 在非 su 時丟 AccessError；`copy` 丟 UserError | IC-R01 |
| `models/document.py`、`folder_access.py`、`document_tag.py`（L3） | 只宣告欄位；`woow_user_can_write` 是 False | IC-R01 |
| `models/document_share.py`（L3） | `_SECRET_SENTINEL`、`SECRET_CTX_KEY` 用最終值；`create` 和 `write` 在非 su 時丟 AccessError；`_check_create_rights` 丟 AccessError；`_woow_mark_revoked` 丟 NotImplementedError | IC-R01 |
| `models/ir_attachment.py`（L3） | 非 su 時：create 的 vals 裡 `res_model` 屬於 `WOOW_MODELS` → AccessError；write 的 vals 只要含 `GUARDED_WRITE_KEYS` 任何一鍵 → AccessError（不分 model） | IC-R01 |
| `models/mail_thread_guard.py`（L3） | `_get_thread_with_access` 對非內部使用者回傳 `self.browse()`，對內部使用者照 core 判斷；`message_post`、`message_subscribe`、`_message_update_content` 對非內部使用者丟 AccessError | IC-R01 |
| `hooks.py`（L3） | `uninstall_hook(env)` 丟 UserError（M-70），一律拒絕解除安裝 | IC-R01 |
| `services/folding.py`（L3） | 函式都回傳「全部折疊」：`display_name` 回傳受限標籤；`breadcrumb` 回傳 `[(None, ELLIPSIS)]`；`roots` 和 `children_of` 回傳 `[]`；`zip_segments` 回傳 `()` | ER-32 |
| `services/url_policy.py`（L3） | `validate` 丟 `ValueError('stub')`；`is_safe` 回傳 False | IC-R01 |
| `controllers/streaming.py`（L4） | `uniform_404`、`too_many_requests`、`service_busy`、`json_response`、`limit_page` 回傳最小的 Response（固定 bytes、status 對應）；`serve`、`create_batch`、`zip_response` 丟 NotImplementedError；`MAX_UPLOAD_BODY`、`max_upload_body` 用最終值 | ER-32 |
| `services/zip_builder.py`（L4） | dataclass 和例外用最終定義；`build_zip` 丟 NotImplementedError | IC-R01 |
| `services/tokens.py`（L5） | `TOKEN_RE`、`MAX_LINK_LEN` 用最終值；`extract_code` 回傳 None；其他丟 NotImplementedError | IC-R01 |
| `models/share_service.py`（L5） | `_resolve_request_access` 回傳 `(self.browse(), None)`（stub 裡沒有任何 sudo）；`_validate_for_access` 回傳 False；`_scope_documents` 回傳空 recordset；公開的 `action_*` 第一個陳述句是 `self.check_access('write')`，接著丟 NotImplementedError | IC-R01 |
| `models/trash_actions.py`、`cron_jobs.py`、`zip_actions.py`、`backend_actions.py`、各精靈 | 公開方法的第一個陳述句是權限檢查，接著丟 NotImplementedError；`_cron_*` 的第一個陳述句是 su 檢查 | IC-R01 |
| controllers（L4、L5、L7） | 只有類別，沒有任何 route。`tools/routes.lock.json` 第 0 天是空清單，各 lane 新增 route 時自己更新 | IC-R12 |

---

## 4. IF-STATIC：static tools、標記、白名單

- **提供者**：L0。
- **使用者**：L1～L7、L11、L12（L8～L10 間接受它管轄）。

### 4.1 指令列介面
- 每支工具都是 `python3 -I tools/check_<name>.py [--g1] [--root <repo>]`。
  - 輸出每行 `<path>:<line> <RULE> <message>`。
  - 結束碼：0＝通過，1＝有違規，2＝工具本身不能用（CI 視為失敗）。
- `tools/run_all.sh [--g1]` 依序跑 8 支，任何一支非 0 就非 0。
- `--g1` 模式：sudo 計數改成「剛好等於」；`WOOW-STUB` 必須是 0；entry points 必須剛好 17 筆，而且等於 target。
- `check_routes.py --emit-entry-points`：輸出 JSON 陣列。每個 decorated route 函式一筆（main 4、portal 5、share 7），再加上固定的一筆 `CustomerPortal._prepare_home_portal_values`，合計 17 筆（ER-47.5、T-STAT-17）。

### 4.2 原始碼標記（同一行的註解）

| 標記 | 意義 |
|---|---|
| `# sudo-ok: S1`／`S2`／`S3` | sudo 白名單 |
| `# sudo-drop: S2` | S2-b 降權 |
| `# sql-ok: Q1`～`Q6` | raw SQL 白名單 |
| `# WOOW-STUB` | L0 寫的 stub，G1 時必須是 0 筆 |
| CSV 那一列旁邊的 `HD-07` | portal 對 folder 的 W=1 只是探測用（check_security_matrix 檢查） |

### 4.3 sudo 白名單（check_sudo；ER-25、ER-44）

| 代號 | 檔案 | model（判斷方式） | 函式 | 數量 |
|---|---|---|---|---|
| S1 | `W/models/document_log.py` | `_name='woow.document.log'` | `_log_event` | 1：`self.sudo().create(vals)` |
| S2 | `W/models/share_service.py` | `_inherit='woow.document.share'`（類別沒有 `_name`；取唯一一個字串型 `_inherit`） | `_resolve_request_access` | 2：(a) `self.sudo().search([(<'token_hash'｜'id'>, v)], limit=1)`；(b) `share_sudo.with_user(share_sudo.create_uid).with_context(allowed_company_ids=[...])` |
| S3 | `W/models/settings.py` | `_name='woow.document.settings'` | `_read_params` | 1：`self.env['ir.config_parameter'].sudo().get_param(k)` |

- 非測試程式裡，除了 S2-b 之外，`with_user(` 是 0 次。
- 整個 repo 不准出現 `<名稱以 _sudo 結尾>.env`（T-STAT-19）。
- 每個 `_cron_*` 的第一個陳述句都必須檢查 `self.env.su`。

### 4.4 raw SQL 白名單（check_sql；`tools/sql_allowlist.yaml`）

| 代號 | 檔案::函式 | 內容 |
|---|---|---|
| Q1 | `models/document_log.py::_log_event` | ER-28 第 2 點的 SQL；衝突鍵的第三欄由 `share_id` 改成純整數欄位 `link_ref`（見變更紀錄），推斷式與索引逐字相同 |
| Q2 | `models/quota.py::_usage_bytes` | arch §6.4 Q2；以自己的連線讀已 commit 的用量（見 §7.2） |
| Q3 | `models/document_folder.py::init`（名稱唯一索引）、`models/document_log.py::init`（denied bucket，ER-28 第 1 點；鍵改用 `link_ref`，並把舊版 denied 列的 `link_ref` 設成各自的 `share_id` 後才建新索引、刪舊索引） | 只准固定字串的 DDL（ER-47.4） |
| Q4 | `services/locks.py`（`try_lock`、`lock`、`bounded_wait`） | `SELECT pg_try_advisory_xact_lock(%s)`、`SELECT pg_advisory_xact_lock(%s)`、`SELECT current_setting('lock_timeout')`、`SELECT set_config('lock_timeout', %s, true)` |
| Q5 | `models/quota.py::_bytes_if_new` | arch §6.4 Q5 |
| Q6 | `models/ir_attachment.py`（守衛） | arch §6.4 Q6 |
| Q7 | `models/folder_access.py::_check_member` | 在存取交易鎖之下，不經 record rule 數這個資料夾的全部授權列（授權上限的判斷） |
| Q8 | `models/folder_access.py::_lock_folders` | 新增授權之前，在 Repeatable Read 下強制列版本衝突，讓並行的授權變更序列化 |
| Q9 | `models/document_log.py::_count_share_unlock_failures` | 建立者範圍內的分享解鎖失敗次數彙總，不暴露受限的來源雜湊 |
| Q11 | `models/share_service.py::_woow_send_link` | 每小時寄信次數計數之前，在 Repeatable Read 下先寫寄件者那一列的版本（屏障） |
| Q12 | `models/share_service.py::_woow_lock_share_row`、`::_woow_count_on_share_row` | 下載次數與錯誤密碼次數計數之前先鎖 share 列（Repeatable Read）；只有計入下載或錯誤密碼時才寫列版本 |
| Q13 | `models/document_log.py::_peek_source`、`::_lock_source`、`::_add_one`、`::_release_source` | 跨分享的來源拒絕計數列（不洩漏分享是否存在）：快照讀、鎖定讀與加一（舊快照會 serialization_failure），以及移除沒有計數的列 |
| Q14 | `models/document_log.py::_take_slot`、`::_short_transaction`、`::_minute_is_full`、`::_reserve` | 每分鐘的拒絕紀錄容量：呼叫交易的快照已顯示該分鐘滿了就直接拒絕，否則在自己的 READ COMMITTED 短交易為呼叫交易的分鐘取一個槽，不超過上限（拒絕自己的短交易用自己的連線，與需要槽的那一列一起取）；這些短交易的隔離等級 |
| Q16 | `models/document_log.py::_denied_bucket_exists` | 已存在的來源每分鐘 bucket（鍵與唯一索引相同，用 `link_ref`）不再占用另一個容量槽 |
| Q17 | `models/ir_attachment.py::_woow_field_attachment_ids`、`::_woow_document_file_row` | 要加進討論的附件中，哪些是某個 binary 欄位的附件；文件記錄的檔案附件（`attachment_id`，且是不帶 URL、屬於該文件 `datas` 的 binary 附件）的儲存位置，串流只從這一列建立 |
| Q18 | `models/mail_thread_guard.py::_woow_follower_scope`、`::_woow_user_companies` | 通知對象所在記錄的讀取規則所依據的儲存欄位（state、公司、eff_* 授權、直接分享），以及被通知帳號的公司 |
| Q19 | `models/document_folder.py::_woow_check_name_free` | 同一層、同公司、啟用中的資料夾是否已用這個名稱（與唯一索引相同的判斷），先回 M-02 |
| Q20 | `models/document.py::_woow_portal_direct_documents` | 直接分享給呼叫者本人聯絡人的文件 id，再以呼叫者的 record rule 搜尋 |
| Q21 | `models/trash_actions.py::_woow_lock_purge_root` | purge 讀任何 hold 之前，以 `SELECT … FOR NO KEY UPDATE` 依 id 順序鎖住這個根資料夾與子樹裡每個資料夾的列（子樹以 active_test=False 的 child_of 取得，含個別丟進回收桶的資料夾；只鎖不寫列版本）：本批快照之後 commit 的 legal hold、還原或搬移改過其中任一列，就 serialization_failure、跳過這個根；之後才開始的會等這一批結束 |
| Q22 | `models/document_log.py::_count_below` | 任何人都能造成的拒絕（無效連結、沒有授權）的來源計數列：一條語句在低於上限時才加一，在自己的短交易（READ COMMITTED）裡立即 commit；新來源的列以 count 1 插入，並在同一個交易取容量槽 |
| Q23 | `models/document_log.py::_bump_denied_bucket` | 同上拒絕的 denied 稽核 bucket（事件、來源、分鐘，不依連結）：已有的 bucket 一條語句加一，新的在同一個短交易插入並取槽 |

- 表以 `tools/sql_allowlist.yaml` 為準（`check_sql --g1` 要求每一筆都被用到），這裡列出它的每個代號；Q10（配額的列版本屏障）與 Q15 已移除，代號不再使用。
- 其他地方出現 `pg_*advisory*`，或 locks.py 裡呼叫內建的 `hash(`，都直接失敗（T-STAT-22）。
- tag、access、`attachment_id`、`token_hash` 的唯一索引，一律用 ORM 或 `odoo.tools.sql.create_unique_index(cr, name, table, [固定運算式])` 建在各自 model 的 `init()` 裡。這不是 raw SQL，不算 Q3（IC-R07）。

### 4.5 check_routes（sec §7，再套用 ER-22、ER-27、ER-47.2）
- R1～R13 照 sec §7，另有以下修改：
  - **R3**：`auth='public'` 只能出現在 `W/controllers/share.py`。
  - **R6**：沒有任何 `<path:>` 例外。
  - **R8**：上傳路由要寫 `max_content_length=95 * 1024 * 1024`（整數字面量的乘法運算式，檢查器把它算成 99,614,720 比對）。如果寫的是 callable，名稱只能是 `max_upload_body`（IC-R03）。
  - **R11**：`auth='public'` 的函式，第一個陳述句（docstring 除外）必須是一個呼叫 `self._share_entry(...)` 的 Assign 或 Expr。`GET /woow-docs/s` 是例外：函式裡不准出現 `request.env['woow.`、`.touch(`、`request.session[...]` 的指派，也不准出現 `_log_event(`（ER-22 第 3 點）。
- `tools/routes.lock.json` 每一列是 `{file, func, routes[], methods[], auth, type, website}`，屬於 always_human 範圍。

### 4.6 check_forbidden（arch §6.2 加 sec §8，再加 ER 的新規則）
- ER-29：在 controllers 裡用 raise、abort 或 return 回傳 not_found（T-STAT-23）。
- ER-35：`force_send=True`（T-STAT-25）。
- ER-04：rules.xml、controllers 和 models 裡出現 `commercial_partner_id`（T-STAT-24）。
- R2b-02、R2b-03：`.touch(`；對 `request.session[...]` 做 subscript 指派、`.pop(`、`.update(` 這類原地修改（T-STAT-21）。

### 4.7 check_security_matrix
- 比對 `tools/security_matrix.yaml`，內容是 arch §4.3 的 ACL 加上 §4.4 的 rule。
- 已經刪除兩個舊模組的 model（ER-02）。
- 不准有 base.group_public 的列。
- log 沒有 W 和 D。
- 每個有 company_id 的 model 都有公司全域 rule。
- `group_woow_document_user` 不能被 base.group_user 隱含（DEC-10）。

### 4.8 check_cleanroom
- 只檢查 ER-33 的 4 件事。
- 排除 `CLEANROOM.md` 和 `tools/tests/fixtures/**`（ER-47.12）。

### 4.9 `tools/public_methods.txt`（每行一筆 `model::method`；check_public_methods）

**[public]**：第一個陳述句必須是 `check_access(`、`_woow_has_rank(`、`_woow_require_rank(`、`has_group(` 或 `_require_manager(` 其中之一。
```
woow.document::action_trash
woow.document::action_restore
woow.document::action_purge
woow.document::action_set_legal_hold
woow.document::action_download_zip
woow.document::action_open_share_wizard
woow.document::action_open_move_wizard
woow.document.folder::action_trash
woow.document.folder::action_restore
woow.document.folder::action_purge
woow.document.folder::action_set_legal_hold
woow.document.folder::action_download_zip
woow.document.folder::action_open_share_wizard
woow.document.folder::action_open_move_wizard
woow.document.folder::action_notify_members
woow.document.share::action_revoke
woow.document.share::action_reissue
woow.document.share.wizard::action_create_links
woow.document.move.wizard::action_move
woow.document.restore.wizard::action_restore_to
woow.document.purge.wizard::action_confirm_purge
woow.document.add.attachment.wizard::action_add
woow.document.notify.wizard::action_send
```

**[override]**：覆寫 core 的公開方法，規則是「必須呼叫 super，或直接丟錯」（IC-R10）。
```
*::create  *::write  *::unlink  *::copy  *::init
woow.document::message_post  woow.document::message_subscribe
woow.document.folder::message_post  woow.document.folder::message_subscribe
res.config.settings::set_values
woow.document.*.wizard::default_get
```

另外兩條規則：AbstractModel（settings、upload.policy、quota、guard.mixin）不准有任何 public method；`@api.model` 的 public method 只准出現在 [override] 清單裡。

---

## 5. IF-CONST：`W/services/constants.py`（純 Python）

- **提供者**：L3。第 0 天由 L0 寫進最終值（IC-R02）。
- **使用者**：L1、L2、L8、L10（ER-47.13），以及實際上所有 lane。

```python
RANK: dict[str, int] = {'read': 1, 'upload': 2, 'edit': 3, 'manage': 4}
LEVELS: tuple[str, ...] = ('read', 'upload', 'edit', 'manage')
PORTAL_LEVELS: tuple[str, ...] = ('read', 'upload')
SUBJECT_TYPES: tuple[str, ...] = ('user', 'group', 'partner')

EVENTS: tuple[str, ...] = (  # 21 種，順序固定（ER-37）
    'view', 'download', 'zip', 'upload', 'replace', 'trash', 'restore', 'purge', 'move',
    'access_change', 'legal_hold', 'share_create', 'share_send', 'share_open', 'share_access',
    'share_download', 'share_unlock_failed', 'share_revoke', 'share_expire', 'settings_change',
    'rate_limited',
)
CHANNELS: tuple[str, ...] = ('backend', 'portal', 'share', 'rpc', 'cron')
RESULTS: tuple[str, ...] = ('ok', 'denied')

WOOW_MODELS: tuple[str, ...] = (
    'woow.document', 'woow.document.folder', 'woow.document.folder.access',
    'woow.document.tag', 'woow.document.share', 'woow.document.log',
)
TRASHABLE_MODELS: tuple[str, ...] = ('woow.document', 'woow.document.folder')   # I-04

# context 鍵
CTX_LOG_CHANNEL = 'woow_log_channel'       # 值只能是 'backend' 或 'rpc'；其他值一律忽略
CTX_LEGAL_HOLD = 'woow_legal_hold'         # action_set_legal_hold(hold=None) 的預設值來源
CTX_SHARE_URL = 'woow_share_url'           # 分享信範本
CTX_SHARE_NOTE = 'woow_share_note'
CTX_NOTICE_URL = 'woow_notice_url'         # 成員通知信範本
CTX_NOTICE_NOTE = 'woow_notice_note'
SESSION_GRANTS_KEY = 'woow_doc_grants'     # session 授權的命名空間（L5）

# 上限和數值
MIB = 1024 * 1024
UPLOAD_HARD_CAP_MB = 95
MAX_UPLOAD_BODY = 95 * 1024 * 1024         # 99_614_720（ER-03、ER-47.2）
MAX_GRANTS = 20
MAX_RECIPIENTS = 20
MAX_SHARE_DOCUMENTS = 500
MAX_ACCESS_ROWS_PER_FOLDER = 500
TREE_QUERY_LIMIT = 1000
PORTAL_PAGE_SIZE = 20
ZIP_DEADLINE_S = 90
ZIP_SPOOL_MAX_BYTES = 32 * MIB
PIXEL_LIMIT = 50_000_000
FILENAME_MAX_CHARS = 200
SEGMENT_MAX_BYTES = 255
URL_MAX_LEN = 2048
LINK_MAX_LEN = 2048                        # ER-12
USER_AGENT_MAX = 256
LOCK_TIMEOUT_MS = 5000
PASSWORD_MIN, PASSWORD_MAX = 8, 128
SHARE_UNLOCK_FAIL_PER_HOUR = 50
SHARE_UNLOCK_LOCK_MINUTES = 60
MAIL_PER_USER_PER_HOUR = 100
FAILED_MAIL_RETENTION_HOURS = 24
SHARE_RECORD_RETENTION_DAYS = 90
PURGE_BATCH = 200
LOG_RETENTION_BATCH = 5000
POISON_ALERT_DAYS = 3
```

注意：`services/locks.py` 也有一個 `RANK`，那是鎖的取得順序（ER-24），和這裡的權限等級是兩回事。引用時一律帶模組名稱，例如 `constants.RANK`、`locks.RANK`。

---

## 6. IF-SET 和 IF-IP

### 6.1 IF-SET：`W/models/settings.py` 和 `W/models/res_config_settings.py`
- **提供者**：L1。
- **使用者**：L2、L3、L4、L5、L6、L7、L8、L12（L11 唯讀）。

```python
PARAM_PREFIX = 'woow_document_management.'
_PARAM_KEYS: tuple[str, ...]          # 下表 30 個鍵（不含前綴），順序同下表
_READABLE_KEYS: frozenset[str] = frozenset(PARAM_PREFIX + k for k in _PARAM_KEYS) | {'database.secret'}

@dataclasses.dataclass(frozen=True)
class WoowDocSettings:   # 欄位名稱＝鍵名稱；型別見下表
    ...

@dataclasses.dataclass(frozen=True)
class ShareKeys:
    k_share: bytes   # HMAC-SHA256(database.secret, b"woow_document_management/share-token/v1")
    k_hint: bytes    # 同上，label 用 b".../share-hint/v1"
    k_ip: bytes      # 同上，label 用 b".../audit-ip/v1"

class WoowDocumentSettings(models.AbstractModel):
    _name = 'woow.document.settings'
    _description = 'Document management settings reader'

    def _read_params(self, keys: Iterable[str]) -> dict[str, str | None]:
        """S3。鍵不在 _READABLE_KEYS → ValueError。只回傳原始字串。"""
    def _get_settings(self) -> WoowDocSettings:
        """型別轉換和範圍檢查。任何鍵失敗，就用下表『讀取失敗時』那一欄的值（fail closed）。不寫任何參數（I-12）。"""
    def _share_keys(self) -> ShareKeys:
        """只在記憶體衍生金鑰。database.secret 是空的或讀不到 → ValueError。"""
```

| 鍵 | 型別（dataclass） | 預設 | 範圍 | 讀取失敗時 |
|---|---|---|---|---|
| trash_retention_days | int | 30 | 0～3650（0＝不自動清除） | 30 |
| share_default_days | int | 14 | 1～share_max_days | 14 |
| share_max_days | int | 90 | 1～90（ER-17） | 90 |
| share_link_policy | str | folder_managers | folder_managers、managers_only | managers_only |
| share_require_password | bool | False | — | True |
| share_session_minutes | int | 60 | 5～480 | 60 |
| share_unlock_max_fail | int | 5 | 3～10 | 5 |
| share_unlock_cooldown_min | int | 15 | 5～120 | 15 |
| denied_rate_per_min | int | 10 | 3～100 | 10 |
| share_link_base_url | str | '' | 必須是 https URL | '' |
| allow_http_links | bool | False | — | False |
| portal_max_upload_mb | int | 25 | 1～95 | 25 |
| internal_max_upload_mb | int | **95** | 1～95 | **95**（ER-03） |
| max_files_per_upload | int | 10 | 1～20 | 10 |
| extra_extensions | tuple[str, ...] | () | 只能是 `mime_map.OPTIONAL_EXTENSIONS` 的子集 | () |
| zip_max_files | int | 2000 | 1～5000 | 2000 |
| zip_max_mb | int | 500 | 1～2048 | 500 |
| zip_max_depth | int | 20 | 1～50 | 20 |
| zip_concurrency | int | 2 | 1～4 | 2 |
| quota_mb | int | 0 | 0～10^7（0＝不限） | 0 |
| quota_warn_pct | int | 80 | 1～99 | 80 |
| quota_block_pct | int | 95 | warn＋1～100 | 95 |
| log_retention_days | int | 365 | 30～3650 | 365 |
| log_ip_mode | str | truncate | truncate、hash、none | truncate |
| allow_root_create | bool | False（ER-05） | — | False |
| tag_create_policy | str | managers | managers、users | managers |
| mail_subject_include_name | bool | False | — | False |
| trusted_proxies | tuple[ip_network, ...] | () | CIDR 清單（經 `client_ip.parse_trusted_proxies` 解析） | () |
| client_ip_header | str | '' | ''、CF-Connecting-IP、X-Real-IP、X-Forwarded-For | '' |
| uninstall_confirm | str | '' | 原樣保留 | '' |

**`res.config.settings`（`_inherit='res.config.settings'`）**
- 欄位一律是 `woow_doc_<key>`，`config_parameter='woow_document_management.<key>'`。不用 related 帶出任何值。
- `set_values(self)` 的流程：
  1. 第一個陳述句：`if not self.env.user.has_group('base.group_system'): raise AccessError`（ER-14）；
  2. 檢查範圍，超出就丟 ValidationError；
  3. 呼叫 `super()`；
  4. 用 `_log_event('settings_change', channel='backend', detail={'changed': n})` 記錄，`n` 是實際有變動的鍵數。

**xmlid（L1）**
- `woow_document_management.res_config_settings_view_form_woow_document`：繼承 `base.res_config_settings_view_form`。
- `woow_document_management.action_woow_document_config_settings`：`ir.actions.act_window`，model 是 res.config.settings，context 帶 `{'module': 'woow_document_management'}`。
- 選單由 L8 掛（§13）。

**交易和 su**：讀取一律走 S3。`set_values` 在呼叫者的交易裡執行，寫入由框架處理。

**L0 stub**：見 §3。

- `res.config.settings.default_get`（`@api.model`，也是表單第一次 `onchange` 的來源）以 superuser 填入 `config_parameter` 欄位，任何已登入使用者都能經 RPC 呼叫，欄位 groups 對它無效：非 su、非 `base.group_system` 的呼叫端，`woow_doc_*` 欄位（`config_parameter` 以 `PARAM_PREFIX` 開頭的）會從要求的欄位清單拿掉，所以信任 proxy 清單、限流與其他設定值不會讀給 portal 或一般內部使用者；系統管理員的設定畫面與儲存不變（`tools/public_methods.txt` 的 [override] 列有 `res.config.settings::default_get`）。

### 6.2 IF-IP：`W/services/client_ip.py`（純 Python，不 import `odoo.models`）
- **提供者**：L1。
- **使用者**：L3（`_log_event`）、L5、L12（G-IP）。

```python
HEADERS: tuple[str, ...] = ('', 'CF-Connecting-IP', 'X-Real-IP', 'X-Forwarded-For')

def parse_trusted_proxies(raw: str) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """用逗號或空白分隔的 CIDR。任何一項不合法 → 回傳 ()（fail closed：只相信 peer）。"""
def client_ip(environ: Mapping[str, str], trusted: tuple, header: str) -> str:
    """arch §15 的演算法。peer 取 ProxyFix 之前的 REMOTE_ADDR。
    只有 peer 在 trusted 裡、而且有設定 header 時，才讀標頭；XFF 從右往左解析。
    不合法 → peer。回傳 ipaddress 正規化之後的字串。"""
def request_client_ip(httprequest, settings: 'WoowDocSettings') -> str:
    """environ = httprequest.environ.get('werkzeug.proxy_fix.orig') or httprequest.environ（CA-14）。"""
def truncate_ip(ip: str) -> str:
    """IPv4 取 /24、IPv6 取 /64，回傳網段字串。不合法 → ''。"""
def hmac_ip(k_ip: bytes, ip: str) -> str:
    """hex64。"""
```

`_log_event` 的處理方式：
- `ip_hash` 永遠是 `hmac_ip(k_ip, 完整 ip)`。
- `ip_display` 依 `log_ip_mode`：truncate → `truncate_ip`；hash → `ip_hash`；none → None。

---

## 7. 上傳、配額、命名、鎖（L2）

### 7.1 IF-UPL：`W/models/upload_policy.py`
- **提供者**：L2。
- **使用者**：L3（`document.create` 和 `write`）、L4、L7、L8（間接）、L9（錯誤訊息的翻譯）。

```python
INLINE_WHITELIST: frozenset[str] = frozenset({
    'image/png', 'image/jpeg', 'image/gif', 'image/webp', 'application/pdf', 'text/plain'})
    # CA-23 不成立時，由 L2 拿掉 'application/pdf'，並記進 docs/CONTRACTS.md（ER-47.14）

REJECT_CODES = ('size', 'filename', 'extension', 'mime', 'content', 'pixels', 'quota',
                'no_libmagic', 'channel', 'stub')   # 'stub' 只給第 0 天用

class WoowUploadRejected(odoo.exceptions.UserError):
    def __init__(self, code: str, **params: int | str): ...
    code: str        # 必須在 REJECT_CODES 裡
    params: dict     # 例如 limit=95、ext='exe'、pct=96；訊息由 code 對到 M-11～M-16、M-19

@dataclasses.dataclass(frozen=True)
class ValidatedFile:
    name: str        # sanitize_filename 之後的名稱
    mimetype: str    # 嗅探結果（不含 charset）
    size: int        # 實際 bytes 長度
    checksum: str    # env['ir.attachment']._compute_checksum(data)（ER-39，不直接用 hashlib.sha1）

class WoowDocumentUploadPolicy(models.AbstractModel):
    _name = 'woow.document.upload.policy'
    def _validate(self, name: str, data: bytes, *, quota: bool = True) -> ValidatedFile:
        """arch §11 的全部步驟。通道由 env.user 決定，不由呼叫端傳入：
        內部使用者（含 su 的 OdooBot）→ internal；share 使用者 → portal；其他 → WoowUploadRejected('channel')。
        單檔上限＝設定的 MiB × MIB，用實際 len(data) 比對（ER-03）。
        quota=True 時最後一步呼叫 env['woow.document.quota']._check(checksum, size)；一次儲存多個檔案的呼叫端（document.create）
        傳 quota=False，驗證完所有檔案之後，再以 _check_many 一起檢查（§7.2）。"""
    def _served_mimetype(self, doc) -> str:
        """ER-36：回傳 doc.mimetype；text/* 加 '; charset=utf-8'；
        不在 mime_map 的可接受集合裡 → 'application/octet-stream'。"""
    def _inline_allowed(self, mimetype: str) -> bool:
        """去掉參數之後的 base type 在 INLINE_WHITELIST 裡才為真。"""
```

**例外**
- 驗證不通過 → `WoowUploadRejected`。它是 UserError 的子類別，所以 RPC 會顯示訊息，controller 會接住，再 return 400 JSON。
- 圖片的寬 × 高超過 `PIXEL_LIMIT` → `'pixels'`。Pillow 開啟圖片時會拿宣告的尺寸和自己的上限比對：超過兩倍上限丟 `Image.DecompressionBombError`（是 `Exception`，不是 `OSError` 或 `ValueError`），超過一倍只警告（警告被設成錯誤時是 `Image.DecompressionBombWarning`）。這兩個也一律是 `'pixels'`，不可往外丟成 500；Pillow 讀不懂的圖片是 `'content'`。
- 配額鎖逾時 → `locks.LockBusy` 原樣往外丟，由 controller 轉成 503。
- PNG、JPEG、GIF 的標頭結構在 Pillow 開啟之前先計數（Pillow 以 Python 逐一讀取，有些結構的成本比大小高：GIF 逐段串接註解、JPEG 保留每個應用程式區段、PNG 保留每個私有 chunk）：PNG 計到第一個 IDAT 為止的 chunk、JPEG 計到 SOS 為止的區段、GIF 計到第一個圖像之前的擴充區塊與其子區塊，超過 `IMAGE_MAX_STRUCTURES`（4096），或結構之外的雜訊位元組超過 `IMAGE_MAX_JUNK`（4096）→ `'content'`，不呼叫 `Image.open`。
- PNG 另有更緊的限制，因為有些 chunk 的成本遠高於大小：Pillow 會解壓色彩設定檔（`iCCP`）和壓縮文字（`zTXt`，以及壓縮的 `iTXt`），每個最多 1 MiB，而幾百位元組就能解成 1 MiB（約 1000 倍），又只限制文字 chunk 的總量（64 MiB，`iCCP` 沒有累計上限）；4000 個約 1 KB 的 `iCCP`（約 4 MB，在每個大小上限之內）會讓 `Image.open` 跑十幾秒。`Image.open` 只讀到第一個 IDAT 為止的 chunk，但 `load()` 讀到 IEND，而 Odoo 儲存附件時會載入 PNG（`ir.attachment` 的後處理讀它的方向或縮圖），所以 IDAT 之後的 chunk 一樣有成本。因此 `_png_structure_ok` 走過整個檔案到 IEND：不是影像資料的 chunk 最多 `PNG_MAX_CHUNKS`（256；一般檔案不到 20 個）個、色彩設定檔最多 `PNG_MAX_PROFILES`（1，PNG 規格只允許一個）個、`zTXt` 加 `iTXt` 最多 `PNG_MAX_COMPRESSED_TEXTS`（16）個；影像資料的 chunk（`IDAT`，動畫的 `fdAT` 與 `fcTL`）個數不限，但函式庫以 Python 逐個讀它們，空的或很小的 chunk 成本遠高於大小，所以超過 `PNG_FREE_IMAGE_CHUNKS`（4096）個之後，平均每個要有 `PNG_IMAGE_CHUNK_BYTES`（256）位元組（這也讓檢查本身走過的 chunk 數有上限）。任何一項超過 → `'content'`，不呼叫 `Image.open`（JPEG 與 GIF 仍用 `IMAGE_MAX_STRUCTURES`）。`tEXt` 的成本等於它的大小（4000 個約 10 毫秒），只受 chunk 總數限制。

### 7.2 IF-QUOTA：`W/models/quota.py`
- **提供者**：L2。
- **使用者**：L3（經由 `_validate`）、L6（cron）。

```python
class WoowDocumentQuota(models.AbstractModel):
    _name = 'woow.document.quota'
    def _usage_bytes(self) -> int:                      # Q2  # sql-ok: Q2
        """已 commit 的用量，以 registry.cursor() 的獨立連線讀取（快照在取得配額鎖之後才取）。
        呼叫端的交易在第一個陳述句就取了快照，早於它等待配額鎖的時間；在那個快照讀用量，會看不到等待期間
        別人 commit 的上傳，所以先前用 UPDATE ir_config_parameter 逼並行上傳 serialization_failure 再重試，
        重試前還會睡眠，一個帳號連續上傳就能讓其他人的上傳變慢、回 500。取不到連線 → LockBusy（503）。"""
    def _bytes_if_new(self, checksum: str, size: int) -> int:   # Q5  # sql-ok: Q5；checksum 已存在就回 0
    def _check(self, checksum: str, size: int) -> None:
        """= _check_many([(checksum, size)])。"""
    def _check_many(self, items: list[tuple[str, int]]) -> None:
        """items 是一次呼叫要儲存的所有檔案的 (checksum, size)，依序累計，每個檔案都算進前面的。
        quota_mb == 0 或沒有檔案 → 直接 return。
        否則：flush ir.attachment → locks.lock(cr, 'quota:u:<uid>') → locks.lock(cr, 'quota')（都在驗證和 checksum 之後才取，
        document.create 在所有檔案驗證完之後才呼叫，所以鎖只在寫入檔案的期間被持有）
        → 新用量 = 已 commit 的用量（一次呼叫量一次）+ 本次執行已接受的位元組 + size
        → 超過 quota_block_pct → WoowUploadRejected('quota')。
        一個帳號同一時間只有一個上傳交易：它的其他請求等自己的 quota:u 鎖，不排在全站配額鎖之前。
        QuotaLedger（context 鍵 LEDGER_KEY）讓同一個交易裡的多次呼叫（加入資料夾精靈、write 同時換多個檔案）
        共用同一次量測，並累計已接受但尚未 commit 的位元組；沒有帶 ledger 的呼叫自己量一次。
        只有加入資料夾精靈（context 帶 SOURCE_CTX_KEY 的 sentinel，來源已 check('read')）以
        _bytes_if_new() 抵扣已存的相同內容；一般上傳一律計入 size，答覆不因全庫是否有相同內容而不同。
        精確百分比只給文件管理員（pct=…），其他人的訊息不含百分比。
        鎖逾時 → LockBusy。"""
    def _cron_check_warn(self) -> None:
        """第一個陳述句：if not self.env.su: raise AccessError。
        用量到 quota_warn_pct 時，對文件管理員建立 activity（M-57），每天最多一次。"""
```

### 7.3 IF-NAME：`W/services/naming.py` 和 `W/services/mime_map.py`（純 Python）
- **提供者**：L2。
- **使用者**：L3、L4、L5、L7。

```python
# naming.py
WINDOWS_RESERVED: frozenset[str]   # CON PRN AUX NUL COM1..9 LPT1..9（不分大小寫）
def sanitize_filename(name: str) -> str:
    """只取 basename（/ 和 \ 都當分隔），NFC，去掉控制字元、雙向字元、':'、NUL，
    去掉開頭的點、空白和 '-'；≤ FILENAME_MAX_CHARS 字元並保留副檔名；Windows 保留名稱加前綴 '_'。
    結果是空字串 → ValueError。"""
def sanitize_segment(name: str, empty_label: str) -> str:
    """ZIP 路徑的一段：NFC，去掉 / \ NUL、C0/C1、U+202A–202E、U+2066–2069；
    去掉 '.'、'..'、開頭的點和空白；utf-8 ≤ SEGMENT_MAX_BYTES；空字串 → empty_label（由呼叫端傳入已翻譯的「未命名」）。"""
def dedupe(names: Sequence[str]) -> list[str]:
    """同一層同名時，加上 ' (2)'、' (3)'…；比對時不分大小寫。"""
class Deduper:
    """一次一個名稱的 dedupe：add(name) 回傳 dedupe 對同一份清單在同一位置給的名稱。已被取走的名稱放進建構子。
    取走的名稱不會再放開，所以記住下一個要試的數字，n 個名稱只做幾個 n 次 _fit_segment（每個名稱：名稱本身一次，
    加上它試的候選），不是 n × n 次。記住的單位是「有同樣編號候選」的名稱，不是「拼法相同」的名稱：
    鍵是 (數字的位數, 為該位數的數字裁短後的主檔名, 副檔名)，後兩者不分大小寫比對（casefold）。
    大小寫不同的名稱、被裁成同一段文字的長名稱（雙位元組字元）、以及只從 10 起才裁成一樣的名稱
    （數字每多一位，主檔名就少一個位元組）都共用同一個計數；每個候選至多屬於兩個鍵（有或沒有副檔名），
    所以一個名稱跳過的都是別的名稱取走的號碼，每個號碼合計至多被跳過兩次。
    zip_builder 每一層一個 Deduper（根層一開始就有 '_links.txt'）。"""
    def __init__(self, taken: Sequence[str] = ()): ...
    def add(self, name: str) -> str: ...

# mime_map.py
DEFAULT_EXTENSIONS: frozenset[str]    # arch §11 的預設白名單
OPTIONAL_EXTENSIONS: frozenset[str]   # 例如 mp4、mov、heic、rtf、7z
BLOCKED_EXTENSIONS: frozenset[str]    # arch §11「永遠封鎖」
EXT_TO_MIMES: Mapping[str, frozenset[str]]
COMPRESSED_MIMES: frozenset[str]      # jpg、png、gif、webp、zip、OOXML、ODF、pdf
def acceptable_mimes(ext: str) -> frozenset[str]: ...
def is_compressed(mimetype: str) -> bool: ...
```

### 7.4 IF-LOCK：`W/services/locks.py`（ER-24、ER-47.3）
- **提供者**：L2。
- **使用者**：L4（ZIP）、L5（max_downloads）、L2（quota）、L6（purge 的 `bounded_wait`，IC-R09）。

```python
class LockBusy(Exception): ...
RANK: dict[str, int] = {'zip': 1, 'share': 2, 'quota': 3}
NAME_RE = re.compile(r'^(zip:u:\d+|zip:slot:[1-4]|zip:s:\d+:[0-9a-f]{16}|share:\d+|quota(?::warn|:u:\d+)?|access:\d+)$')

def advisory_key(name: str) -> int:
    """int.from_bytes(hashlib.sha256(b'woow_docmgmt/lock/v1:' + name.encode()).digest()[:8], 'big', signed=True)
    名稱不符合 NAME_RE → ValueError。不准用內建 hash()（T-UPL-31）。"""
def try_lock(cr, name: str) -> bool:
    """SELECT pg_try_advisory_xact_lock(%s)  # sql-ok: Q4。成功就登記 rank。"""
def lock(cr, name: str, timeout_ms: int = LOCK_TIMEOUT_MS) -> None:
    """with cr.savepoint(flush=False):
         prev = SELECT current_setting('lock_timeout')
         SELECT set_config('lock_timeout', %s, true)   -- f'{int(timeout_ms)}ms'
         SELECT pg_advisory_xact_lock(%s)
         SELECT set_config('lock_timeout', %s, true)   -- 還原 prev（IC-R08）
       遇到 psycopg2.errors.LockNotAvailable → savepoint rollback → raise LockBusy。
       交易仍然可用，所以 S1 log 還能 commit。"""
@contextlib.contextmanager
def bounded_wait(cr, timeout_ms: int) -> Iterator[None]:
    """在 savepoint 裡把 lock_timeout 暫時設成 timeout_ms，離開時還原。
    區塊內遇到 row lock 逾時 → savepoint rollback → raise LockBusy。
    給 purge cron 跳過被鎖住的列用（取代 SKIP LOCKED，IC-R09）。不取 advisory lock，不登記 rank。"""
def _reset_tracker(cr) -> None:
    """測試用。tests/common.py 的 setUp 呼叫（ER-47.3）。"""
```

**rank 追蹤器**
- 用 `weakref.WeakKeyDictionary[cursor, int]` 記錄目前持有的最高 rank。
- 第一次登記時，用 `cr.postcommit.add(clear)` 和 `cr.postrollback.add(clear)` 註冊清除動作。
- 要取的鎖 rank 低於目前持有的最高 rank → `AssertionError`，視為程式錯誤。同 rank 可以。
- TestCursor 會忽略 post-commit 回呼（`sql_db.py` L607），所以測試一律靠 `_reset_tracker`。

**呼叫端**

| 呼叫端 | 用法 | 取不到時 |
|---|---|---|
| L4 ZIP | `try_lock('zip:u:<uid>')` 或 `try_lock('zip:s:<share_id>:<ip_hash[:16]>')` → 對 i＝1..zip_concurrency 逐一 `try_lock('zip:slot:<i>')` | 回傳 `limit_page('M-42' 或 'M-43', 429)` |
| L5 分享下載 | `lock(cr, 'share:<id>')` | `LockBusy` → `service_busy()` |
| L2 配額 | `lock(cr, 'quota:u:<uid>')`，再 `lock(cr, 'quota')`（同為 rank 3） | `LockBusy` → 由路由回傳 `service_busy()` |

---

## 8. 核心 model、權限、log、守衛（L3）

### 8.1 IF-MODEL
- **提供者**：L3。
- **使用者**：L4、L5、L6、L7、L8、L10、L11。

**guard mixin（`models/guard_mixin.py`）**
```python
class WoowDocumentGuardMixin(models.AbstractModel):
    _name = 'woow.document.guard.mixin'
    _woow_allowed_defaults: frozenset[str] = frozenset()   # 各 model 覆寫，例如 {'default_folder_id'}
    @api.model_create_multi
    def create(self, vals_list):
        """清掉 context 裡不在白名單的 default_*。
        非 su 時，補做欄位 groups 檢查（CA-10），沒權限 → AccessError。su 時略過（ER-45.4）。"""
    def copy(self, default=None):          # folder、document、share 一律丟 UserError
    def _require_manager(self) -> None:     # 不是 su，也不在 group_woow_document_manager → AccessError
    def _woow_log_channel(self) -> str:     # su → 'cron'；share 使用者 → 'portal'；context 是 backend 或 rpc 就用它；其他 → 'rpc'
```

**`woow.document.folder`（`models/document_folder.py`）**
- 欄位照 arch §3.1，**另外有以下這些**（其他 lane 會用到的名稱一律照這裡寫）：
  - 10 個 eff 欄位：`eff_read_user_ids`、`eff_upload_user_ids`、`eff_edit_user_ids`、`eff_manage_user_ids`、`eff_read_group_ids`、`eff_upload_group_ids`、`eff_edit_group_ids`、`eff_manage_group_ids`、`eff_read_partner_ids`、`eff_upload_partner_ids`。rel 表是 `woow_doc_folder_eff_{r,u,e,m}_{user,group}_rel` 和 `woow_doc_folder_eff_{r,u}_partner_rel`。
  - `in_trash`、`subtree_restored_at`、`has_restricted_descendant`、`subtree_legal_hold`：stored、recursive。`has_restricted_descendant`、`subtree_legal_hold` 以 `with_context(active_test=False).child_ids` 計算：個別丟進回收桶的子資料夾也算，儲存值不受重算時 context 影響；`init()` 在每次安裝與更新時呼叫 `_woow_recompute_subtree_flags()` 重算所有資料夾的這兩個欄位。
  - `woow_user_rank`：Integer，非 stored，`@api.depends_context('uid')`，值是 0～4。文件管理員和 su 是 4。
  - `woow_has_portal_members`：Boolean，非 stored，條件是 `bool(eff_read_partner_ids)`（ER-15）。
  - `effective_access_lines`：Json。`document_count`、`subfolder_count`：在使用者 env 用 `_read_group` 計算。
  - 舊模組的 `policy_flag` **不存在**（ER-36）。

```python
    @api.model
    def _woow_rank_domain(self, n: int) -> list:
        """E(n)，內容見 arch §4.4。"""
    def _woow_filter_rank(self, n: int) -> 'woow.document.folder':
        """self 裡通過 _woow_has_rank(n) 的資料夾，只用一次 search（一個分享範圍的幾千份文件不再每份查一次）。"""
    def _woow_has_rank(self, n: int) -> bool:
        """ensure_one。= bool(self._woow_filter_rank(n))：su 或文件管理員 → self.exists()（仍然套用公司 rule）。
        內部使用者 → search([('id','in',self.ids)] + E(n))。非內部使用者 → 空。"""
    def _woow_require_rank(self, n: int) -> None:
        """self 裡任何一筆 _woow_has_rank(n) 不成立 → AccessError（訊息不含名稱）。"""
    def _woow_portal_can_upload(self) -> bool:
        """非內部使用者：bool(self._filtered_access('write'))，也就是 rule R-F6。內部使用者 → False。"""
    def _woow_breadcrumb(self) -> list[tuple[int | None, str]]:
        """依 parent_path 和使用者 env 的 search，交給 folding.breadcrumb 計算。"""
    @api.model
    def _woow_portal_roots(self, limit: int = TREE_QUERY_LIMIT) -> 'woow.document.folder':
    def _woow_portal_children(self, limit: int = TREE_QUERY_LIMIT) -> 'woow.document.folder':
    @api.model_create_multi
    def create(self, vals_list):
        """ER-45.3：if not vals.get('parent_id') and not self.env.su and not 文件管理員
        and not settings.allow_root_create: raise AccessError(M-24)（_woow_require_root_right）。
        非文件管理員不能以 company_id 為空建立（跨公司共用資料夾只有文件管理員能建立，US-23 AC4）。
        不寄信，不自動加 follower（ER-45.4）。"""
    def write(self, vals):
        """非 su：company_id 只有文件管理員能改（改成別家公司或改成空值；寫入目前的值不算變更），
        只有 manage 的成員不行：根資料夾改成空值或別家公司之後，子資料夾可以逐層改，等於把整棵子樹交給別家公司的管理員。
        parent_id 清空（搬到最上層）而 self 裡有資料夾原本有上層 → 和 create 一樣的根資料夾政策（M-24），
        先於名稱檢查，所以同名與否的答案相同；原本就在最上層的不擋。"""
    def init(self):
        """Q3（固定字串）：woow_document_folder_name_uniq ON
        (COALESCE(parent_id,0), COALESCE(company_id,0), lower(name)) WHERE active。  # sql-ok: Q3"""
```

**`woow.document.folder.access`（`models/folder_access.py`）**
- 欄位：`folder_id`、`company_id`、`subject_type`、`user_id`、`group_id`、`partner_id`、`level`。
- 限制照 arch §3.2。唯一索引用 `create_unique_index` 建（IC-R07）。
- 改到 access 會記 `access_change`。建立時不寄信、不加 follower（ER-45.4）。

**`woow.document`（`models/document.py`）**
- 欄位照 arch §3.4，**刪除 `policy_flag`**。另外有：
  - `woow_user_can_write`：Boolean，非 stored，值等於 write rule 是否成立；
  - `SOURCE_CTX_KEY = 'woow_doc_source'`，搭配 `_SOURCE_SENTINEL = object()`（module-level）。只有 context 帶這個 sentinel 時，才准寫 `source_res_model` 和 `source_res_id`。L8 的加入資料夾精靈從 document.py import 它。

```python
    @api.model_create_multi
    def create(self, vals_list):
        """I-10 的唯一入口：
        - vals 有 datas → data = base64.b64decode(datas) → upload.policy._validate(name, data) → 寫入 name 和 mimetype；
        - 非 su 時，client 帶 mimetype、attachment_id、checksum、file_size → AccessError；
        - 非內部使用者只接受 name、folder_id、datas、description，伺服器設定 uploaded_by_partner_id，強制 tracking_disable；
        - 內部使用者帶 mail_create_nolog 和 mail_create_nosubscribe；
        - 每份文件用 S1 記一筆 upload。"""
    def write(self, vals):   # datas → _validate、記 replace；folder_id → 目標 ≥ upload、不在回收桶、同公司，記 move；shared_* 需要 manage
    def unlink(self):        # 只有文件管理員或 su；有 legal_hold → UserError(M-34)
```

**`woow.document.tag`（`models/document_tag.py`）**
- create 依 `tag_create_policy` 檢查，su 時略過。

**ER-45.4 的承諾（同時也是 IF-RB-2、IF-RB-3）**
- guard mixin 的欄位 groups 檢查在 `env.su` 時略過。
- 建立 folder 和 access 時，不寄任何信、不自動加 follower。成員通知只能透過 L8 的精靈。
- 這兩項如果有任何改動，L3 都要通知 L10，L10 再重跑 T-RB-02～T-RB-05。

### 8.2 IF-SHARE-SCHEMA：`models/document_share.py`
- **提供者**：L3。
- **使用者**：L5、L6、L11。

```python
_SECRET_SENTINEL = object()
SECRET_CTX_KEY = 'woow_doc_share_secret'

class WoowDocumentShare(models.Model):
    _name = 'woow.document.share'
    _inherit = ['woow.document.guard.mixin']      # 不繼承 mail.thread
    # 欄位照 arch §3.6；token_hash 和 password_hash 的 groups 是 base.group_system
    @api.model_create_multi
    def create(self, vals_list):
        """非 su 時：context[SECRET_CTX_KEY] 不是 _SECRET_SENTINEL，而 vals 有 token_hash 或 password_hash → AccessError。
        每一筆都呼叫 self._check_create_rights(vals)。"""
    def write(self, vals):
        """非 su 時只准寫 name，以及縮短 expire_at；其他欄位一律 AccessError。"""
    def _check_create_rights(self, vals: dict) -> None:
        """L3 的版本一律丟 AccessError（fail closed）。L5 在 share_service.py 覆寫。"""
    def _woow_mark_revoked(self) -> None:
        """呼叫端已經做過 check_access('write')。
        用 super().write 寫 revoked_at、revoked_by_id、state='revoked'。撤銷後不能復活。"""
```

### 8.3 IF-FOLD：`W/services/folding.py`（純 Python）
- **提供者**：L3。
- **使用者**：L4、L5、L7（L3 自己的 display_name 也用它）。

```python
ELLIPSIS = '…'
Crumb = tuple[int | None, str]      # (folder_id 或 None, label)；None 代表不能點的「…」

def parse_parent_path(parent_path: str) -> tuple[int, ...]: ...
def fold(path_ids: Sequence[int], readable: Mapping[int, str]) -> list[Crumb]:
    """看得到的節點顯示名稱；連續看不到的節點合併成一個 (None, ELLIPSIS)。"""
def display_name(path_ids: Sequence[int], readable: Mapping[int, str],
                 restricted_label: str, sep: str = ' / ') -> str:
    """自己看不到 → restricted_label。"""
def breadcrumb(path_ids: Sequence[int], readable: Mapping[int, str]) -> list[Crumb]: ...
def roots(candidates: Mapping[int, tuple[int, ...]]) -> list[int]:
    """candidates 是可讀的 folder id → 祖先 id。回傳沒有任何可讀祖先的那些 id。"""
def children_of(folder_id: int, candidates: Mapping[int, tuple[int, ...]]) -> list[int]:
    """回傳「最近的可讀祖先就是 folder_id」的那些 id。"""
def zip_segments(root_id: int | None, path_ids: Sequence[int], readable: Mapping[int, str]) -> tuple[str, ...]:
    """只放看得到的名稱，看不到的中間層直接省略（I-08）；名稱還沒經過 sanitize_segment。"""
```

### 8.4 IF-LOG：`models/document_log.py`
- **提供者**：L3。
- **使用者**：L1、L4、L5、L6、L7、L8、L11。

```python
class WoowDocumentLog(models.Model):
    _name = 'woow.document.log'
    @api.model
    def _log_event(self, event: str, *, result: str = 'ok', channel: str,
                   user_id: int | None = None, partner_id: int | None = None,
                   share_id: int | None = None, document_id: int | None = None,
                   folder_id: int | None = None, company_id: int | bool | None = None,
                   ip: str | None = None, user_agent: str | None = None,
                   detail: Mapping[str, int | bool | list[int]] | None = None) -> int:
        """S1 加 Q1。
        - event、result、channel 不在 constants 的清單裡 → ValueError（屬於程式錯誤）；
        - detail 的值只能是整數、布林值或整數清單，否則 ValueError；
        - ip 沒有傳入，而且有 http request 時，用 client_ip.request_client_ip 取得；user_agent 截斷到 256；
        - user_id 預設是 env.uid（public 使用者記 None）；company_id 預設（None）是 env.company.id，False（沒有公司的資料夾，
          `folder.company_id.id` 讀出來就是 False）記成空公司，R-L0 讓每家公司的管理員都讀得到；其他型別 → ValueError。
        result == 'ok'：self.sudo().create(vals)   # sudo-ok: S1   → 回傳 1。
        result == 'denied'：flush_model() → Q1（ER-28 第 2 點；衝突鍵用 `COALESCE(link_ref, 0)`，見變更紀錄）  # sql-ok: Q1
                           → invalidate_model() → 回傳 RETURNING count（同一個分鐘桶的累計數）。
        denied 列不寫 document_id 和 folder_id，需要時放進 detail。"""
    def init(self):    # Q3：ER-28 第 1 點的唯一索引，現為 woow_document_log_denied_link_bucket（鍵用 link_ref）  # sql-ok: Q3
```

**限流判斷**：呼叫端拿 `count > settings.denied_rate_per_min` 判斷，成立就 return `too_many_requests()`，並另外記一筆 `rate_limited`（denied）。

**任何人都能造成的拒絕（`ATOMIC_EVENTS`＝`share_access`）**走 `_refuse(event, ip, *, limit, channel='share')`：來源計數列由一條語句在低於上限時才加一（`_count_below`，Q22），audit bucket 由一條語句加一（`_bump_denied_bucket`，Q23），各在自己的短交易（READ COMMITTED，`registry.cursor()`，立即 commit）裡，所以同一來源的並行請求不互相等待、不 serialization_failure、不被 core 睡眠重試；計入的恰好是上限筆，其餘回 429。新列在同一個短交易取容量槽，沒有槽就不建立該列（槽數＝寫入的列數，不因並行而浪費）。回傳 `'counted'`（回 404）、`'limited'`（已記 `rate_limited`，回 429）或 `'busy'`（新來源沒有容量、輪次或連線：回 429，沒有寫任何東西）。錯誤密碼（`share_unlock_failed`）仍走鎖定的來源計數列，在 `share_unlock` 的鎖之下判斷。取不到連線時所有這類拒絕都回 429。

**事件 → 記錄的 lane**

| 事件 | lane |
|---|---|
| upload、replace、move、access_change | L3 |
| view、download、zip | L4 的 `serve` 和 `zip_response`（portal 也經由它們） |
| trash、restore、purge、legal_hold | L6 |
| share_create、share_send、share_open、share_access、share_download、share_unlock_failed、share_revoke、share_expire、rate_limited | L5（share_expire 由 L6 的 cron 經 `_maintenance_run` 觸發） |
| settings_change | L1 |

- legal_hold 的 detail 是 `{"on": 0或1, "doc": id}` 或 `{"on": 0或1, "folder": id}`（ER-37）。
- 分享 ZIP 記 `share_download`，detail 是 `{"zip": 1, "files": n, "bytes": b}`，計入 max_downloads（IC-R11）。

### 8.5 IF-URL：`W/services/url_policy.py` 和 `W/views/safe_url_templates.xml`
- **提供者**：L3。
- **使用者**：L8；L5 和 L7 的範本也會用到。

```python
def validate(url: str, *, allow_mailto: bool = False) -> str:
    """回傳正規化之後的 URL；不合法 → ValueError(code)，code 用來顯示 M-30。"""
def is_safe(url: str, *, allow_mailto: bool = False) -> bool: ...
```
- QWeb：`woow_document_management.safe_url_link`，用 `t-call` 帶 `url` 和 `label`。合格時輸出 `<a target="_blank" rel="noopener noreferrer nofollow">`，不合格只輸出文字。
- MVP 一律 `allow_mailto=False`，不提供設定鍵（IC-R13）。

### 8.6 IF-GUARD：`models/ir_attachment.py` 和 `models/mail_thread_guard.py`
- **提供者**：L3。
- **使用者**：L11（獨立測試）；L4 和 L8 的行為依賴它。

**ir.attachment 守衛**
- 只 `_inherit='ir.attachment'`，覆寫 `create(vals_list)` 和 `write(vals)`，**不碰** `unlink`、`check`、`_search`（I-03）。
- 守衛只對非 su 生效（下面「寫入後的目標」一條例外，su 也適用）：
  - **create**：`res_model in WOOW_MODELS` 而且符合以下任一條件 → AccessError：`res_field` 有值、`public` 為真、帶 `access_token`。
  - **write**：
    - Q6 查出 self 裡哪些是「本模組 model 的欄位附件」。對這些列，寫入 `GUARDED_WRITE_KEYS = ('raw','datas','db_datas','mimetype','type','url','res_model','res_id','res_field','public','access_token','store_fname')` 任何一鍵 → AccessError。
    - 對本模組 model 的 chatter 附件（res_model 用目前使用者的 ORM 讀取），寫 `public`、`access_token`、`res_model`、`res_id`、`res_field` → AccessError。
    - 把 `res_model` 改成本模組 model、同時設定 `res_field` → AccessError。
  - `generate_access_token()` 和 `copy()` 會走 write 或 create，所以一起被擋（core 的 generate_access_token 不是 sudo，`ir_attachment.py` L667–676）。
- **寫入後的目標（所有 env，含 su）**：write 帶 `res_model`、`res_id` 或 `res_field`，而寫入後 `res_model` 是本模組 model 時，`_woow_check_field_target` 以 Q6 讀出每列原本的（res_model, res_id, res_field）：寫入後帶 `res_field`、且與原本的目標不同 → AccessError。core 以 su 把發文者的待送附件改掛到記錄上（`_process_attachments_for_post`，`message_post` 與可指定任何 `model`／`res_id` 的 `message_notify` 都會走到），binary 欄位只建立附件、從不搬移，所以沒有任何途徑能讓文件或資料夾多出一筆欄位附件。

**mail_thread_guard（ER-20）**
```python
class _Guard(models.AbstractModel):   # 實際上分別 _inherit woow.document 和 woow.document.folder
    def _get_thread_with_access(self, thread_id, mode="read", **kwargs):
        """不呼叫 super。
        非內部使用者 → self.browse()。
        內部使用者 → thread = self.browse(thread_id)；thread.exists() and thread.sudo(False).has_access(mode) 才回傳 thread，
        否則 self.browse()。永遠不走 portal 的 hash、pid、token fallback（portal/models/mail_thread.py L96–103）。"""
    def message_post(self, **kwargs):           # 非內部使用者 → AccessError，否則 super
    def message_subscribe(self, *args, **kwargs):
    def message_unsubscribe(self, partner_ids=None):   # 非 su 的非內部使用者只能移除自己
    def _message_update_content(self, *args, **kwargs):
    @api.model
    def message_new(self, msg_dict, custom_values=None):   # 一律 AccessError：不從信件建立記錄
    def _process_attachments_for_post(self, ...):   # attachment_ids 有 res_field 的 → AccessError（Q17）
    def _notify_get_recipients(self, message, msg_vals, **kwargs):
        """follower 有帳號、又不是這則訊息明確指定的收件者時，依 Q18 的儲存欄位重驗讀取規則
        （R-F0～R-F5、R-D0～R-D5 的鏡像，不切換身分）；讀不到就不通知。沒有帳號的聯絡人照 core（HD-10）。"""
    # 在 folder 上：write() 對非內部使用者一律 AccessError（HD-07）；
    # message_post 的 record_name、_message_compute_subject、mail.activity 的 res_name、
    # mail.message._get_record_name 都只用資料夾自己的名稱，不帶上層路徑（I-08）。
```
- `mail.thread._message_route_process`：路由目標是這兩個 model 時，只有 su（fetchmail）或 `base.group_system`（mail gateway 腳本）能處理；其他人經 `message_process` 送信一律 AccessError。
- `ir.binary`（`models/ir_binary.py`）：`_find_record` 對本模組 model 的記錄，只要帶 `access_token`（參數或 query）就 MissingError，`/web/content` 回 404、`/web/image` 回 placeholder，回應不會被標成 public；`_record_to_stream` 對 `woow.document.datas` 一律從文件自己的 `attachment_id` 建立串流（Q17 讀出儲存位置；必須是不帶 URL、屬於該文件 `datas` 的 binary 附件，否則 MissingError）。下載、預覽、portal、分享、ZIP 與 `/web/content`、`/web/image` 都經過這裡，其他宣稱同一欄位的附件即使存在也不會被送出。
- `message_ids`、`website_message_ids`、`has_message`（mail.thread 的欄位，在 `woow.document` 與 `woow.document.folder` 上）加 `groups='base.group_user'`：auto_join 的 one2many 在 domain 裡以子查詢直接查 `mail_message`，不經 R-M0，portal 使用者能對自己讀得到的記錄測試內部 log note 的文字；`guard_mixin._search` 與 `_read_group_groupby` 對 domain 與 groupby 的第一段欄位名檢查欄位 groups，portal 因此得到 AccessError，內部使用者不變。
- `ir.binary._record_to_stream` 對文件的 `datas` 回傳 `_DocumentStream`：類型固定為上傳政策對這個檔案提供的類型（`_served_mimetype` 的 base type，由回應補 charset），`ir.binary._get_stream_from` 指派的 `mimetype`（`/web/content`、`/web/image` 的 `?mimetype=`）無效；只有 `INLINE_WHITELIST` 的類型可以 inline，其餘一律 attachment；CSP 固定為 `default-src 'none'; sandbox`。`res_field` 屬於本模組 model 的附件 id（`/web/content/<附件 id>`、`/web/image/<附件 id>`）→ MissingError：文件的檔案只有經由文件這一條路由。
- **ir.attachment write** 另依「寫入後的值」判斷：`res_model` 改成本模組 model 時，若該列原本或寫入後帶 `res_field`、`public` 或 `access_token` → AccessError（兩步驟搬移也擋）。

### 8.7 IF-UNINST：`W/hooks.py`
- **提供者**：L3。
- **使用者**：L0（manifest 的 `uninstall_hook`）、L6（README 的 legal hold 段落）、L12。

```python
def uninstall_hook(env) -> None:
    """env 是 superuser（CA-28）。依序：
    1. 以 active_test=False 查 folder 和 document，只要有任何一筆 legal_hold → UserError(M-71)，訊息只附筆數（ER-30）；
    2. 還有任何 woow.document（含已封存的），而且 uninstall_confirm 不等於 database.uuid → UserError(M-70)。
       database.uuid 用 env['ir.config_parameter'].get_param 讀取（env 是 su，不加 sudo）；
    3. 以 ORM 依序刪除 documents → folders（由深到淺）→ shares；
    4. 刪除 PARAM_PREFIX + _PARAM_KEYS 的所有參數。
    只用 ORM，沒有 raw SQL。"""
```

---

## 9. IF-STREAM、IF-ZIP、IF-ROUTE-INT（L4）

### 9.1 IF-STREAM：`W/controllers/streaming.py`（模組層級函式，不是 Controller 類別）
- **提供者**：L4。
- **使用者**：L5、L7、L11、L12。

```python
from ..services.constants import MAX_UPLOAD_BODY        # 99_614_720
def max_upload_body(controller=None) -> int:            # ER-47.2 的 callable 簽名；回傳 MAX_UPLOAD_BODY
    ...

def uniform_404(kind: Literal['share', 'page']) -> Response:
    """status 404。
    body 用 eager render：request.env['ir.ui.view']._render_template('woow_document_management.uniform_404_<kind>', {})，
    只依請求的語言而定，不放任何記錄資料、使用者名稱或 csrf_token。
    同一個 kind、同一個語言的 body bytes 完全相同（I-07）。
    標頭：kind='share' 套用 share 標頭，kind='page' 套用 page 標頭。"""
def too_many_requests() -> Response:        # 429，M-66 頁
def service_busy() -> Response:             # 503，M-45 頁
def limit_page(message_id: Literal['M-41','M-42','M-43','M-44'], status: int, **params) -> Response:
    """ZIP 的上限或忙碌頁面。M-41 用 400，M-42、M-43 用 429，M-44 用 503。"""
def json_response(payload: dict, status: int = 200) -> Response:
    """application/json，套用 no-store 和 nosniff。"""
def apply_common_headers(response: Response, kind: Literal['file', 'page', 'share', 'portal', 'json']) -> Response:
    """一律加 Cache-Control: no-store, private 和 X-Content-Type-Options: nosniff。
    file：CSP default-src 'none'; sandbox，加 Cross-Origin-Resource-Policy: same-origin；
    share：Referrer-Policy no-referrer、X-Robots-Tag noindex, nofollow、CSP frame-ancestors 'none'；
    portal：CSP frame-ancestors 'self'（T-PORTAL-18）。"""
def serve(doc, *, inline: bool, channel: Literal['backend', 'portal', 'share'],
          event: Literal['download', 'view'] | None, share_id: int | None = None) -> Response:
    """呼叫端已經在 doc 所屬的 env 做過權限檢查（內部、portal，或分享建立者的 env）。
    stream = doc.env['ir.binary']._get_stream_from(doc, 'datas', filename=doc.name,
                                                    mimetype=policy._served_mimetype(doc))
    as_attachment = not (inline and channel != 'share' and policy._inline_allowed(mimetype))
    resp = stream.get_response(as_attachment=…, content_security_policy="default-src 'none'; sandbox")
    檔名經 content_disposition() 處理並去掉 CR/LF；套用 file 標頭；event 有值就記 S1。
    AccessError 或 MissingError → return uniform_404('page'；channel 是 share 時用 'share')。"""
def create_batch(folder, uploads: Sequence['werkzeug.datastructures.FileStorage'], *,
                 channel: Literal['backend', 'portal']) -> Response:
    """folder 屬於使用者 env。
    - 檔數 > max_files_per_upload → 400 JSON，訊息 M-19；
    - 讀出 bytes；在一個 savepoint 裡 Document.create([{name, folder_id, datas: b64}...])；
    - WoowUploadRejected、ValidationError、UserError → rollback savepoint → 400
      {"ok": false, "message": <M-15>, "errors": [{"index": i, "code": code, "message": <M-10>}]}；
    - AccessError → uniform_404('page')；LockBusy → service_busy()；
    - 成功 → 200 {"ok": true, "document_ids": [...]}。"""
def zip_response(documents, *, folders=None, root_folder=None,
                 channel: Literal['backend', 'portal', 'share'],
                 share_id: int | None = None, ip_hash: str | None = None) -> Response:
    """documents 和 folders 是呼叫端 env（使用者 env 或 scope_env）裡已經 search 過的 recordset。
    鎖（§7.4）：share → zip:s:<share_id>:<ip_hash[:16]>；其他 → zip:u:<uid>；接著取 slot。
    路徑用 folding.zip_segments 加 naming.sanitize_segment 組成。
    讀任何 bytes 之前先用 stored 的 file_size 檢查上限。
    tempfile.SpooledTemporaryFile(max_size=ZIP_SPOOL_MAX_BYTES)，用 wrap_file 加 call_on_close，出錯時在 finally 關檔。
    ZipLimitExceeded → limit_page('M-41' 或 'M-44')；channel 不是 share 時記 zip（detail 是 files 和 bytes）。"""
```

**L0 stub**：見 §3。T-ZIP-27 驗證 `uniform_404()` 回傳的是 Response 實例。

- `create_batch`：每個上傳在讀取之前先 `upload.stream.seek(0)`（請求因並行錯誤被重跑時，框架只會倒回第一個檔案，其餘會讀成空檔案）。`zip_response` 不在列出文件時建立串流：`ZipEntry.open_stream` 是 `_lazy_stream(doc)` 回傳的函式，`build_zip` 寫到那個項目才呼叫 `_get_stream_from`；檔案存在資料庫（`ir_attachment.location = db`）時串流建立就會把整個檔案讀進記憶體，所以大小上限要在讀任何一個檔案之前，以 `file_size` 判斷（§9.1 原有規則）。非 path／data 的串流在這時才丟 MissingError → `uniform_404`。

### 9.2 IF-ZIP：`W/services/zip_builder.py`（純 Python）
- **提供者**：L4。
- **使用者**：L5、L7、L8。

```python
@dataclasses.dataclass(frozen=True)
class ZipEntry:
    path_segments: tuple[str, ...]          # 已經過 sanitize_segment
    open_stream: Callable[[], BinaryIO]
    size: int
    compressible: bool
@dataclasses.dataclass(frozen=True)
class ZipLimits:
    max_files: int; max_bytes: int; max_depth: int; deadline_s: int = ZIP_DEADLINE_S
@dataclasses.dataclass(frozen=True)
class ZipStats:
    files: int; bytes: int; links: int
class ZipLimitExceeded(Exception):
    def __init__(self, code: Literal['files', 'bytes', 'depth', 'deadline']): ...
def limits_from_settings(settings: 'WoowDocSettings') -> ZipLimits: ...
def build_zip(entries: Sequence[ZipEntry], limits: ZipLimits, fileobj: BinaryIO,
              links: Sequence[str] = ()) -> ZipStats:
    """先檢查上限，再讀 bytes；同一層同名用 dedupe；links（url 類文件）寫進 '_links.txt'；
    壓縮過的類型用 ZIP_STORED，其他用 ZIP_DEFLATED level 6；allowZip64=True。"""
```

### 9.3 IF-ROUTE-INT：`W/controllers/main.py`（4 個 decorated 函式）
- **提供者**：L4。
- **使用者**：L8（OWL 上傳和預覽）、L11、L12。

```python
class WoowDocumentController(http.Controller):
    @http.route('/woow_document/upload', type='http', auth='user', methods=['POST'],
                max_content_length=95 * 1024 * 1024)
    def woow_upload(self, **post) -> Response:
        """不是內部文件使用者 → uniform_404('page')。folder_id 用 int() 加範圍檢查；files = request.httprequest.files.getlist('files')。
        browse → exists → check_access('read') → create_batch(channel='backend')。
        超過 body 上限時，werkzeug 回 413，DB 沒有任何寫入（T-UPL-21）。"""
    @http.route('/woow_document/doc/<int:document_id>/download', type='http', auth='user', methods=['GET'])
    def woow_download(self, document_id: int, **kw) -> Response:     # 內部和 portal 都可以；serve(inline=False, event='download')
    @http.route('/woow_document/doc/<int:document_id>/preview', type='http', auth='user', methods=['GET'])
    def woow_preview(self, document_id: int, **kw) -> Response:      # serve(inline=True, event='view')
    @http.route('/woow_document/zip', type='http', auth='user', methods=['GET'])
    def woow_zip(self, document_ids: str = '', folder_ids: str = '', **kw) -> Response:
        """只限內部使用者。逗號分隔的整數，總數 ≤ zip_max_files。
        在使用者 env 取 ids 和 search 結果的交集，筆數不同 → uniform_404('page')。"""
```

**`models/zip_actions.py`（_inherit document 和 folder）**
- `action_download_zip(self) -> dict`
  - 第一個陳述句：`self.check_access('read')`。
  - active_ids 和使用者 env 的 search 取交集，不同 → AccessError。
  - 回傳 `{'type': 'ir.actions.act_url', 'url': '/woow_document/zip?document_ids=…&folder_ids=…', 'target': 'download'}`。

**QWeb（`views/common_templates.xml`）**
- `uniform_404_share`、`uniform_404_page`（M-65）、`too_many_requests_page`（M-66）、`service_busy_page`（M-45）、`zip_limit_page`（M-41～M-44）。
- 這些都是獨立的最小 HTML，不呼叫 `web.layout`、`portal.portal_layout`，沒有 inline script。

---

## 10. 分享（L5）：IF-TOKEN、IF-SHARE、IF-ROUTE-SHARE、IF-MAIL

### 10.1 IF-TOKEN：`W/services/tokens.py`（純 Python；passlib）
- **提供者**：L5。
- **使用者**：L5 自己、L11。

```python
TOKEN_RE = re.compile(r'^[A-Za-z0-9_-]{43}$')
MAX_LINK_LEN = 2048
def new_token() -> str:                                   # secrets.token_urlsafe(32)
def token_hash(k_share: bytes, token: str) -> str:        # HMAC-SHA256(k_share, token).hexdigest()
def token_hint(k_hint: bytes, token: str) -> str:         # HMAC(k_hint, token).hexdigest()[:6]
def verify(k_share: bytes, token: str, stored_hash: str) -> bool:   # hmac.compare_digest
def extract_code(raw: str) -> str | None:
    """ER-12：len(raw) > 2048 → None；有 '#' → 取最後一個 '#' 後面那段；strip；
    不 fullmatch TOKEN_RE → None。'#' 前面的主機或路徑一律不看。"""
def hash_password(password: str) -> str:   # CryptContext(['pbkdf2_sha512'])；長度不在 8～128 → ValueError
def check_password(password: str, stored: str) -> bool: ...
```

### 10.2 IF-SHARE：`W/models/share_service.py`（ER-44）
- **提供者**：L5。
- **使用者**：L6（cron、legal hold）、L8（按鈕、精靈）、L11。

```python
class WoowDocumentShareService(models.Model):
    _inherit = 'woow.document.share'          # 沒有 _name（check_sudo 依 ER-44 第 2 點判斷）

    @api.model
    def _issue(self, vals: dict) -> tuple['woow.document.share', str]:
        """產生 token 和 hash，context 帶 {SECRET_CTX_KEY: _SECRET_SENTINEL} 呼叫 create；記 share_create。
        明文 token 只出現在回傳值裡（I-05）。"""
    def _check_create_rights(self, vals: dict) -> None:
        """DEC-09：建立者是內部文件使用者；share_link_policy 是 folder_managers 時，每個目標資料夾都要 rank 4，
        是 managers_only 時要在管理員群組，否則 UserError(M-52)；目標不在回收桶；同公司；
        expire_at 必須 now < expire_at ≤ now + share_max_days（M-50）。"""
    @api.model
    def _resolve_request_access(self, token: str | None = None, share_id: int | None = None
                                ) -> tuple['woow.document.share', 'odoo.api.Environment | None']:
        """S2，剛好 2 行：
          share_sudo = self.sudo().search([('token_hash', h)], limit=1)   # sudo-ok: S2
              （或 [('id', int(share_id))]）
          scope_env = share_sudo.with_user(share_sudo.create_uid).with_context(
              allowed_company_ids=[share_sudo.company_id.id]).env      # sudo-drop: S2
        找不到 → return (self.browse(), None)。token 的情況下另外用 tokens.verify 比對。
        建立者已被刪除（create_uid 為空）→ 同樣 return (self.browse(), None)：with_user(空 recordset) 會保留 superuser 的 env，
        _validate_for_access 會把請求的使用者當成建立者，列出整個範圍的文件名稱。
        一律回傳 tuple。呼叫端禁止使用 share_sudo.env。"""
    def _validate_for_access(self, scope_env) -> bool:
        """self 是 share_sudo。第一步（B2）：`scope_env is None`、或 `_woow_check_scope_env(scope_env)` 不成立（su、uid 不是建立者、公司不符）→ False。
        接著依序檢查 arch §8.5 第 1～5、7 步，任何一步不成立就回傳 False，不 raise。"""
    def _scope_domain(self) -> list: ...                                 # arch §8.5 第 6 步
    def _scope_documents(self, scope_env, extra_domain: list | None = None) -> 'woow.document':
        """在 scope_env 裡 search，所以會套用建立者的 rule、回收桶和公司。"""
    def _woow_link_url(self, token: str) -> str:
        """share_link_base_url 或 get_base_url()，接上 '/woow-docs/s#' + token。
        不是 https 而且沒開 allow_http_links → UserError(M-54)；host 是 IP 字面量 → UserError(M-54)。"""
    def _woow_send_link(self, token: str, note: str | None) -> None:
        """template.with_context(woow_share_url=…, woow_share_note=…).send_mail(self.id, force_send=False,
        email_values={'email_to': self.recipient_email, 'recipient_ids': [], 'email_from': sender, 'auto_delete': True},
        email_layout_xmlid='mail.mail_notification_light')；記 share_send。取不到寄件人 → UserError(M-55)。"""
    download_count = fields.Integer(compute=…)   # 在使用者 env 從 log 的 share_download（ok）計算
    def action_revoke(self) -> bool:        # 第一個陳述句：self.check_access('write')；_woow_mark_revoked；記 share_revoke
    def action_reissue(self) -> dict:       # check_access('write')；撤銷舊的，用同一個範圍 _issue 新的；回傳 client action
    def _maintenance_run(self) -> None:
        """第一個陳述句：if not self.env.su: raise AccessError。內容是 arch §8.6（state 改 expired 並記 share_expire、
        刪除到期或撤銷超過 90 天的分享、刪除失敗超過 24 小時的 mail.mail 並建立 activity M-56）。"""

class WoowDocumentShareButtons(models.Model):   # 兩個類別，分別 _inherit 'woow.document' 和 'woow.document.folder'
    def action_open_share_wizard(self) -> dict:  # 第一個陳述句：self.check_access('read')；接著檢查 DEC-09
```

**精靈 `woow.document.share.wizard`（`wizards/share_wizard.py`）**
- `action_create_links(self) -> dict` 回傳 `{'type': 'ir.actions.client', 'tag': 'woow_document_share_result', 'params': {'links': [...]}}`。
- 明文 token 只放在回應的 params 裡。password 不儲存：精靈列 create / write 時立即以 `tokens.hash_password` 存成 `password_hash`（長度 8～128 於儲存時以 UserError 檢查），成功後清空，不記 log；呼叫端不得直接提供 `password_hash`（AccessError）。

### 10.3 IF-ROUTE-SHARE：`W/controllers/share.py`（7 個 decorated 函式，全部 `auth='public'`、`type='http'`）
- **提供者**：L5。
- **使用者**：L9、L11、L12（G-IP、G-CACHE）。

```python
class ShareAccess(NamedTuple):
    share_sudo: 'woow.document.share'   # 只能讀分享自己的欄位
    scope_env: 'odoo.api.Environment'
    ip: str
    ip_hash: str
    grant: dict | None

class WoowDocShareController(http.Controller):
    def _share_entry(self, *, token: str | None = None, share_id: int | None = None,
                     require_grant: bool = True, require_unlocked: bool = True,
                     resolve: bool = True) -> 'ShareAccess | Response':
        """所有 public 路由的第一個陳述句（R11），GET /woow-docs/s 除外。
        失敗一律回傳 Response，不 raise：
        - 拒絕事件用 _log_event(..., result='denied') 記錄；回傳的 count > denied_rate_per_min → too_many_requests()，
          否則 → uniform_404('share')；
        - token 路徑：tokens.extract_code(raw) 是 None → 記 denied 並回 404；之後才做 S2 和 _validate_for_access；
        - 有效 token 和已授權的 session，永遠不會因為 IP 桶滿了被擋（arch §15）；
        - id 路徑（require_grant=True）：先讀 session 裡這個 id 的授權（_session_grant）；沒有、過期或格式不對 → 直接 _denied，
          不做 S2 和 _validate_for_access。不存在、已撤銷與有效的 id 做一樣的事（查詢數相同，所以時間也相同）、回一樣的答案；
          有授權時才解析、驗證分享，再比對 h 與 pw；
        - resolve=False（只給 /close 用）：不呼叫 S2，也不碰 DB，回傳 ShareAccess(share_sudo=空 recordset, scope_env=None, …)。"""

    @http.route('/woow-docs/s', type='http', auth='public', methods=['GET'])
    def share_landing(self, **kw) -> Response:
        """ER-22：可以 request.render('woow_document_management.share_landing')，內嵌 request.csrf_token()。
        不碰 woow model、不寫資料、不 touch、不指派 session、不記 log。"""
    @http.route('/woow-docs/s/open', type='http', auth='public', methods=['POST'])
    def share_open(self, **post) -> Response:
        """raw = post.get('token') or post.get('link') or ''（ER-12）；永遠不讀 query string（T-SHARE-21）。
        成功：request.session.should_rotate = True；把授權寫進 session（見下方）；記 share_open；
        303 轉址到 '/woow-docs/s/%d' % share.id（request.redirect(local=True)）。"""
    @http.route('/woow-docs/s/<int:share_id>', type='http', auth='public', methods=['GET'])
    def share_page(self, share_id: int, **kw) -> Response:        # 記 share_access；需要密碼時顯示密碼表單（M-62）
    @http.route('/woow-docs/s/<int:share_id>/unlock', type='http', auth='public', methods=['POST'])
    def share_unlock(self, share_id: int, **post) -> Response:
        """冷卻檢查在 pbkdf2 之前：同一個 (share, ip_hash) 失敗 share_unlock_max_fail 次，冷卻 share_unlock_cooldown_min 分鐘；
        同一個分享每小時失敗 50 次，鎖 60 分鐘（M-64）。失敗記 share_unlock_failed（M-63）。
        成功設 pw=True，並再換一次 sid。"""
    @http.route('/woow-docs/s/<int:share_id>/file/<int:document_id>', type='http', auth='public', methods=['GET'])
    def share_file(self, share_id: int, document_id: int, **kw) -> Response:
        """文件必須在 _scope_documents 的結果裡。lock(cr, 'share:<id>') → 計算 share_download 的 ok 筆數，
        和 max_downloads 比對 → 記 share_download → serve(doc_in_scope_env, inline=False, channel='share', event=None)。
        document_id 來自網址：不在資料庫 integer 的範圍（1～2**31-1）內的 id 不可能有列，視為範圍裡找不到：
        不查詢，和其他找不到的 id 一樣走 _denied（404，計入來源計數、受限流），不可讓資料庫報錯而回 500。"""
    @http.route('/woow-docs/s/<int:share_id>/zip', type='http', auth='public', methods=['GET'])
    def share_zip(self, share_id: int, **kw) -> Response:
        """zip_response(channel='share', share_id=…, ip_hash=…)。取鎖順序：zip:s → slot → share（IC-R11）。"""
    @http.route('/woow-docs/s/<int:share_id>/close', type='http', auth='public', methods=['POST'])
    def share_close(self, share_id: int, **post) -> Response:     # _share_entry(resolve=False)；從 session 移除授權
```

**session 授權（ER-39、R2b-03）**
- 鍵是 `request.session[SESSION_GRANTS_KEY][str(share_id)]`，值是 `{'exp': int(epoch 秒), 'h': token_hash[:16], 'pw': bool}`。
- `exp = min(now + share_session_minutes, expire_at)`。
- 一律先 `g = dict(request.session.get(SESSION_GRANTS_KEY) or {})`，修改之後整個重新指派。最多保留 MAX_GRANTS 筆，超過時丟掉最舊的，過期的順手清掉。
- 不碰 `session.uid`、`pre_uid`、`authenticate`、`finalize`。
- `share_close` 只在 session 確實持有這個 id 的授權時才重新指派（寫回 session）；沒有授權的請求不寫 session：落地頁的表單 token 屬於尚未儲存的 session id，任何人都能用 GET 落地頁加 POST close 兩個請求換一個 session 檔，違反「未驗證的請求不持久化 session」（R2b-02）。

**公開分享事件的身分**：`share_open`、`share_access`、`share_download` 以請求的環境（`request.env`）記錄，也就是訪客本人（匿名訪客的 `user_id` 為空，已登入的內部訪客記自己），不用建立者的 `scope_env`（`user_id` 與 `create_uid` 都會變成分享建立者，稽核無法歸責）。建立者的 `scope_env` 只用來決定範圍與計算 `max_downloads`；建立者仍可經 R-L1 的 `share_id.create_uid` 讀到這些列。`share._record_download(scope_env, log_env, ip, detail)` 的 `log_env` 是必填。

**拒絕的計數**：`_denied` 對 `ATOMIC_EVENTS` 走 `log._refuse`（§8.4），不等待、不因同一來源的其他請求失敗；`share_file` 的 `document_id` 範圍檢查、`_share_entry` 的順序不變。

**分享頁的公司名稱（ER-16）**
- 用 `scope_env['res.company'].browse(share_sudo.company_id.id).name`。
- 頁面不出現任何 `/web/image` 網址，也沒有 logo。

**QWeb 範本**：`share_landing`、`share_page`、`share_password`（都在 `views/share_templates.xml`）。落地頁只載入 `woow_document_management.assets_share`。

**landing.js（`static/src/share/landing.js`）**
- 讀 `location.hash` → 符合 TOKEN_RE 才繼續 → `history.replaceState` 到固定路徑 → 用欄位 `token` 自動 POST。
- 只用 `textContent`。不用 storage、console、`postMessage`。
- 停用 JS 時，`<form method="post" action="/woow-docs/s/open">` 有一個文字欄位 `link`（`maxlength=2048`、`autocomplete=off`），訊息是 M-61 的新文案。

### 10.4 IF-MAIL：`W/data/mail_templates.xml`（`noupdate="1"`，程式永遠不寫入，I-14）
- **提供者**：L5。
- **使用者**：L8（member_notice）、L9。

| xmlid | model | 用到的 context 鍵 | 寄送方 |
|---|---|---|---|
| `woow_document_management.mail_template_woow_document_share_link` | woow.document.share | `woow_share_url`、`woow_share_note` | L5 的 `_woow_send_link` |
| `woow_document_management.mail_template_woow_document_member_notice` | woow.document.folder | `woow_notice_url`、`woow_notice_note` | L8 的 `notify.wizard.action_send`：內部成員一批（帶後台網址），portal 成員一批（帶 `/my/docs/folder/<id>`）；信裡沒有 token |

- 兩者一律 `send_mail(..., force_send=False)`，收件人在每次寄送時傳入（ER-35）。
- `woow_document_management.action_woow_document_share`：`ir.actions.act_window`，model 是 woow.document.share，定義在 `views/share_views.xml`（L5）。L8 在選單上引用它。
- OWL client action 的 tag 是 `woow_document_share_result`（`static/src/share_result/`，L5）。

---

## 11. IF-TRASH、IF-CRON（L6）

### 11.1 IF-TRASH：`W/models/trash_actions.py`（_inherit document 和 folder）
- **提供者**：L6。
- **使用者**：L8（按鈕和 view）、L11。

```python
def action_trash(self) -> bool:
    """文件：第一個陳述句 self.check_access('write')。
    資料夾：第一個陳述句 self._woow_require_rank(4)；has_restricted_descendant 時只有文件管理員能做（M-33，HD-05）。
    記 trash。"""
def action_restore(self) -> bool | dict:
    """文件：第一個陳述句 self.with_context(active_test=False).check_access('write')。原資料夾不能用時，回傳還原精靈（M-32）。
    資料夾：第一個陳述句 self.with_context(active_test=False)._woow_require_rank(4)（ER-13）；
    上層也在回收桶時，回傳還原精靈。記 restore。"""
def action_purge(self) -> dict:              # 第一個陳述句 self._require_manager()；回傳 purge 精靈（M-35）
def action_set_legal_hold(self, hold: bool | None = None) -> bool:
    """第一個陳述句 self._require_manager()。hold 是 None 時，取 context[CTX_LEGAL_HOLD]（預設 True）。
    記 legal_hold，detail 照 ER-37 第 4 點。"""
def _woow_purge(self) -> tuple[int, int]:
    """內部用，只有 su 或文件管理員能呼叫。由深到淺；legal_hold 或 subtree_legal_hold → M-34；
    每筆包在 savepoint 和 locks.bounded_wait 裡，失敗的記 purge（denied，detail 只放 id）後跳過。回傳 (done, failed)。
    同一次執行中已隨上層一起刪除的記錄直接略過，不算 done 也不算 failed。
    資料夾的每個根先以 _woow_lock_purge_root（Q21）鎖住自己與子樹裡每個資料夾的列（含個別丟進回收桶的），再讀 active 與子樹的 hold；
    文件的 hold 與刪除是同一列。所以在本交易快照之後 commit 的 legal hold、還原或搬移（子樹任何一個資料夾），會讓該根或該文件
    serialization_failure（並行死結同樣處理），這一筆算 failed、整批照常 commit。
    資料夾另有 _woow_purge(budget=None)：budget 是這次最多刪除的記錄數（文件和資料夾都算）；
    放不下的子樹保留在回收桶、只刪掉一部分，下次呼叫接著刪。
    每一筆的任何例外（不只資料庫錯誤，例如沒有公司的記錄先前讓 _log_event 丟 ValueError）都只讓這一筆算 failed、跳過，整批照常 commit；
    記錄只寫 id 與例外的類別，不寫例外的文字（可能含資料）。_woow_purge(failures=None)、_woow_purge_batch(budget=None, failures=None)
    的 failures 是呼叫端給的清單，跳過的記錄以 (model, id) 附加進去。"""
```
- legal hold 依 US-15 AC2 是**項目層級**：保護設了 hold 的項目本身，以及子樹裡有 hold 的資料夾；資料夾設 hold 不保護其中被個別丟進回收桶的文件或子資料夾（另案：是否改成保全資料夾下全部內容，由負責人決定）。
- 精靈：`woow.document.restore.wizard.action_restore_to(self) -> dict`、`woow.document.purge.wizard.action_confirm_purge(self) -> dict`。兩者的第一個陳述句都是權限檢查。

### 11.2 IF-CRON：`W/models/cron_jobs.py` 和 `W/data/ir_cron.xml`
- **提供者**：L6。
- **使用者**：L2（`_cron_check_warn`）、L5（`_maintenance_run`）、L11、L12。

| xmlid | 方法（在 `woow.document` 上，`@api.model`） | 頻率 | 內容 |
|---|---|---|---|
| `ir_cron_woow_document_purge_trash` | `_cron_purge_trash()` | 每天 | 每批最多刪除 PURGE_BATCH 筆記錄（文件和資料夾都算，不論回收桶資料夾多大）；`ir.cron._notify_progress(done=刪除筆數, remaining=)`；失敗的（含本批快照之後才 commit 的 legal hold、還原）跳過、整批照常 commit，不算進 remaining；同一筆連續失敗 POISON_ALERT_DAYS 天，就對管理員建立 activity（R2b-06）。實作：每日的 cron 會試每一筆保留期已過的記錄，所以本次失敗、而且保留期結束已超過 POISON_ALERT_DAYS 天（`trashed_date < cutoff - POISON_ALERT_DAYS 天`）的記錄，就是已連續失敗那麼多天；每位文件管理員每天最多一個 activity（掛在自己的聯絡人上，只寫筆數，不寫名稱），建立失敗只記 log、不讓 purge 失敗 |
| `ir_cron_woow_document_share_maintenance` | `_cron_share_maintenance()` | 每小時 | 呼叫 `share._maintenance_run()` |
| `ir_cron_woow_document_log_retention` | `_cron_log_retention()` | 每週 | 每批 LOG_RETENTION_BATCH 筆 |
| `ir_cron_woow_document_quota` | `_cron_quota()` | 每天 | 呼叫 `quota._cron_check_warn()` |

- 所有 cron 都 `user_id=base.user_root`。
- 每個 `_cron_*` 的第一個陳述句都是 `if not self.env.su: raise AccessError(...)`。

---

## 12. IF-PORTAL：`W/controllers/portal.py` 和 `W/views/portal_templates.xml`

- **提供者**：L7。
- **使用者**：L9、L11、L12。

```python
class WoowDocumentPortal(CustomerPortal):
    def _prepare_home_portal_values(self, counters):
        """呼叫 super；'woow_document_count' in counters 時，在使用者 env 用 search_count 計算。不覆寫 /my/counters。"""
    @http.route(['/my/docs', '/my/docs/page/<int:page>'], type='http', auth='user', website=True, methods=['GET'])
    def portal_my_docs(self, page: int = 1, **kw) -> Response:
    @http.route(['/my/docs/folder/<int:folder_id>', '/my/docs/folder/<int:folder_id>/page/<int:page>'],
                type='http', auth='user', website=True, methods=['GET'])
    def portal_my_docs_folder(self, folder_id: int, page: int = 1, **kw) -> Response:
    @http.route(['/my/docs/file/<int:document_id>', '/my/docs/file/<int:document_id>/download'],
                type='http', auth='user', website=True, methods=['GET'])
    def portal_my_docs_file(self, document_id: int, **kw) -> Response:
        """路徑以固定字串 '/download' 結尾 → serve(inline=False, channel='portal', event='download')；
        否則顯示文件頁（IC-R14）。"""
    @http.route('/my/docs/folder/<int:folder_id>/zip', type='http', auth='user', website=True, methods=['GET'])
    def portal_my_docs_zip(self, folder_id: int, **kw) -> Response:
    @http.route('/my/docs/folder/<int:folder_id>/upload', type='http', auth='user', website=True,
                methods=['POST'], max_content_length=95 * 1024 * 1024)
    def portal_my_docs_upload(self, folder_id: int, **post) -> Response:
        """先用 _woow_portal_can_upload() 決定要不要繼續，不成立 → uniform_404('page')；
        接著 create_batch(channel='portal')。單檔上限由 IF-UPL 依 portal 設定驗證。"""
```

- 每一條失敗路徑都是 `return streaming.uniform_404('page')`。
- 所有回應都套用 `apply_common_headers(kind='portal')`。
- 範本：`woow_document_management.portal_my_home_woow_docs`（繼承 `portal.portal_my_home`）、`portal_my_docs`、`portal_my_docs_folder`、`portal_my_docs_file`，全部用 `portal.portal_layout`，只用 `t-out`。
- 根資料夾和子資料夾用 `_woow_portal_roots` 和 `_woow_portal_children`（底層是 IF-FOLD）；麵包屑用 `_woow_breadcrumb`。
- `static/src/portal/upload.js` 用 fetch 送出 multipart 和 `csrf_token`，回應用 `textContent` 顯示。
- 不依賴 website 模組，`/zh_TW/` 前綴也能用。MVP 沒有 chatter（DEC-11）。

---

## 13. IF-UI：後台 xmlid（L8）

- **提供者**：L8。
- **使用者**：L9、L11；L1 的設定選單由 L8 掛。

| xmlid（`woow_document_management.` 前綴） | 種類 | groups／備註 |
|---|---|---|
| `menu_woow_document_root` | 選單 | App 根選單：en「Documents」、zh_TW「文件」；groups 是 group_woow_document_user 和 group_woow_document_attachment_overview |
| `menu_woow_document_config` | 選單 | Configuration，groups 是 group_woow_document_manager |
| `menu_woow_document_settings` | 選單 | 引用 L1 的 `action_woow_document_config_settings`；`groups="base.group_system"`（ER-14） |
| `menu_woow_document_share` | 選單 | 引用 L5 的 `action_woow_document_share` |
| `action_woow_document_folder`、`action_woow_document_document` | act_window | — |
| `action_woow_document_trash` | act_window | 資料夾的還原按鈕只對 `woow_user_rank == 4` 顯示 |
| `action_woow_document_log`、`action_woow_document_tag` | act_window | — |
| `action_woow_document_attachment_overview` | act_window（ir.attachment） | groups 只有 group_woow_document_attachment_overview；`create="0" edit="0" delete="0"` |
| `action_woow_document_add_attachment_wizard` | act_window（`binding_model_id`＝ir.attachment） | groups 是 group_woow_document_user |

**public method（`models/backend_actions.py`、`wizards/*`）**
- `action_open_move_wizard(self) -> dict`（document 和 folder；第一個陳述句是 `check_access('write')`）。
- `action_notify_members(self) -> dict`（folder；第一個陳述句是 `self._woow_require_rank(4)`）。
- 精靈：`action_move`、`action_add`、`action_send`，都回傳 dict。
- 精靈收到的 `active_ids` 一律在使用者 env 做 `browse → exists → search([('id','in',ids)])`，筆數不同就丟 AccessError。

**其他**
- OWL 欄位 widget 的名稱是 `woow_safe_url`。
- HD-10 提醒放在 folder 和 document form 的 chatter 上方，條件是 `woow_has_portal_members`，文案照 ER-15。
- searchpanel 的 `hierarchize` 依 CA-22 的結果決定，不成立就用 `"0"`。

---

## 14. IF-RB：一次性重建腳本（L10 site-rebuild，repo 外）

- **提供者**：L10（腳本介面，腳本本身不在 repo）。另外 L3 承擔模組端的承諾 IF-RB-1～5。
- **使用者**：L11（覆核）、L12（runbook、G6）。

### 14.1 通則（腳本的細節不在公開 repo）
- 腳本在 superuser 的 odoo shell 執行，只用 ORM，不寫 raw SQL；不受 repo 的 check_sudo 和 check_sql 管轄。
- 模式只有 plan（唯讀，最後 rollback）、apply（單一交易，commit 之前先驗證）和 undo。
- 只建立根資料夾和「檢視」等級的成員；任何人都不會拿到上傳、編輯或管理。
- 輸出只有 id 和計數，**永遠不印**名稱、login 或 email。
- 驗證不通過就 rollback；exit code：0 OK 或 noop、1 計數不符、2 例外、3 STOP、4 驗證失敗。
- 測試 ID：T-RB-01～T-RB-12（repo 外執行）。

### 14.2 L3 對 L10 的承諾（IF-RB-1～5）
1. **IF-RB-1**：ER-45.3 的根資料夾檢查在 su 時略過（§8.1）。T-ACL-57 要包含 su 的案例。
2. **IF-RB-2**：guard mixin 的欄位 groups 檢查在 su 時略過。
3. **IF-RB-3**：建立 folder 和 access 時，不寄信、不加 follower。
4. **IF-RB-4**：
   - `_woow_has_rank(n)` 和 `_woow_portal_can_upload()` 的語意照 §8.1；
   - 群組 xmlid 是 `woow_document_management.group_woow_document_user` 和 `…_manager`；
   - access 的欄位名稱是 `folder_id`、`subject_type`、`user_id`、`partner_id`、`level`。
5. **IF-RB-5**：eff_* 在 flush 時重算。

上面任何一項改了，L3 都要通知 L10，L10 再重跑 T-RB-02～T-RB-05。

## 15. IF-REL：cloud-odoo 打包與平台關卡（ER-47.13：提供者是 L12 自己）

- **提供者**：L12。
- **使用者**：L12 自己；L11 交量測素材；L0 交 G0～G4 的 repo 部分。

1. **上游**：`WOOWTECH/Woow_odoo_document_management`，tag `v18.0.1.0.0`。
2. **同步**：`python3 scripts/addons-sources.py sync --repo https://github.com/WOOWTECH/Woow_odoo_document_management --ref v18.0.1.0.0 --target addons-cloud --modules woow_document_management --expect-commit <G4 last_sha>`。
3. **清單**：`config/cloud-addons.list` 只加一行 `woow_document_management`，不加進 `paas-addons.list`。之後重新產生 catalog 和 requirements。
4. **檢查**：
   - `check-cloud-runtime.py`；
   - `check-addon-lists.sh`，擋 `sh_` 開頭的模組（T-REL-01）；
   - SBOM 擋 AGPL、OPL、PyMuPDF、Ghostscript、ONLYOFFICE（T-REL-02）；
   - Helm values lint 擋 `sh_` 開頭的模組。
5. **發布**：`/approve` 一律由人送出。G5 的 digest 和沙盒不同時，L11 要在新 digest 重跑 G1 和 G1-CA（ER-19 第 3 點）。
6. **獨立站**：改 Helm values 的 init 清單，釘 G5 的 SHA，然後 helm upgrade、重啟、`-i` 或 `-u`。不用 kubectl 直接改。
7. **G-CACHE**（ER-10）：
   - Cache Rule 的條件是 `http.request.uri.path contains "/web/content"`、`"/woow-docs/"`、`"/woow_document/"`、`"/my/docs"` 四者 OR，動作是 bypass；
   - WAF 對 `/woow-docs/` 依 IP 限流，最低要求是 10 秒 60 次；
   - 驗收是 T-E2E-04；
   - 上線時間：在 GM7 的 `/approve` 之前，而且在 G6 之前。
8. **`docs/release/v18.0.1.0.0.md` 欄位**：
   - G0～G5、G1-CA、G1-COV（`sha256:` digest、百分比、各目錄的數字）、G-IP、G-COOKIE、G-CACHE；
   - SC 執行紀錄：T-SHARE-32、44、45、46、47、48（L5），T-UPL-32（L2），T-TRASH-10（L6），T-UNINST-03、05（L3），sc_ca*（L11）。
   - 不寫站名、筆數和主機名稱；registry 只寫 `sha256:` 開頭的 digest（ER-47.9）。
9. **repo 外的檔案**：`ops/release/{owner-actions,g6-runbook,g6-record}.md`、`site-values.patch`、`ops/g-cache/{ruleset.json,apply.sh,verify.sh}`、`ops/g-ip/verify.sh`、`ops/g-cookie/verify.sh`、`ops/residual-check/**`（T-REL-06，唯讀，只印計數）。

---

## 16. PR-01、PR-02、PR-14（ER-47.13）

| 代號 | 定義 | 提供者 → 使用者 | 驗收 |
|---|---|---|---|
| **PR-01** | `W/security/groups.xml` 由 L0 擁有並在第 0 天定稿：`module_category_woow_document`；`group_woow_document_user`（implied `base.group_user`；base.group_user **不隱含**它，DEC-10）；`group_woow_document_manager`（implied group_woow_document_user）；`group_woow_document_attachment_overview`（implied `base.group_user`）。**不寫 `users` 欄位**。admin 的指派交給 PR-14 | L0 → 所有 lane（ACL、rule、選單、L10） | T-ACL-51、T-SEC-79（check_security_matrix） |
| **PR-02** | T-STAT-15 `test_smoke_core` 由 L11 擁有，放在 `W/tests/test_core_assumptions.py`（ER-47.6：test_static_checks 涵蓋 T-STAT-01～13、16～25）。L0 只負責「骨架可以在 基底 DB 副本安裝」 | L11（L0 提供可安裝的骨架） | T-STAT-15 只測一個模組，測試數 > 0 |
| **PR-14** | `W/data/admin_assignment.xml` 由 L3 擁有，內容是 `<odoo noupdate="1"><function model="res.groups" name="write" eval="[[ref('woow_document_management.group_woow_document_manager')], {'users': [(4, ref('base.user_admin'))]}]"/></odoo>`。依 core 的 `convert.py` L257–259，noupdate 檔案裡的 `<function>` 只在 `mode == 'init'` 執行，所以 `-u` 或 `-u all` 不會把已經被移除的 admin 加回去（ER-38） | L3 → L10（owner 的計算）、L11 | T-ACL-54 `test_admin_assignment_noupdate`；T-SEC-89 |

---

## 17. IF-TEST：共用測試介面（L11）

- **提供者**：L11。L0 合併後立刻交 `tests/common.py`。
- **使用者**：L3～L10。

### 17.1 `W/tests/common.py`
```python
class WoowDocFixtureMixin:
    pw: str                                   # setUpClass 時用 secrets.token_urlsafe(24) 產生；測試裡不准寫死密碼
    companies: dict[str, 'res.company']       # {'c1': …, 'c2': …}
    users: dict[str, 'res.users']             # 鍵是 O A U R B N V M S P1 P1u P1s P2 C2 MC（tp §2.1）
    folders: dict[str, 'woow.document.folder']   # Root F G H X Y Z K L XSS TRAV（tp §2.2）
    docs: dict[str, 'woow.document']
    @classmethod
    def _woow_core_ready(cls) -> bool:        # L3 合併前回傳 False，這時只建使用者和公司
    def env_as(self, role: str) -> 'odoo.api.Environment': ...
    def assert_uniform_404(self, responses: Sequence) -> None:   # 比對 status 和 body bytes
    def full_text_scan(self, secret: str) -> list[tuple[str, str]]:
    @contextlib.contextmanager
    def second_connection(self): ...          # 另開一條原始的 psycopg2 連線，用來測 advisory lock 的競爭
    def oracle_visible(self, role: str, model: str) -> set[int]: ...
    @staticmethod
    def make_pdf() -> bytes: ...
    @staticmethod
    def make_png(w: int = 2, h: int = 2) -> bytes: ...
    @staticmethod
    def make_txt(text: str = 'synthetic') -> bytes: ...

class WoowDocCase(WoowDocFixtureMixin, odoo.tests.TransactionCase):
    def setUp(self):                           # 呼叫 super()，再呼叫 locks._reset_tracker(self.env.cr)（ER-47.3）
class WoowDocHttpCase(WoowDocFixtureMixin, odoo.tests.HttpCase): ...
```
- 寄信一律用 mail 模組的測試 mock。
- fixture 只用合成資料，IP 只用 192.0.2.0/24、198.51.100.0/24、203.0.113.0/24、2001:db8::/32 和 127.0.0.1（ER-47.9）。

### 17.2 tag、docstring、檔案
- tag 一律是 `@tagged('post_install', '-at_install', 'woow_docmgmt', 'woow_docmgmt_<core|share|portal|files>')`。
- `test_perf.py` 用 `('woow_docmgmt_perf', '-standard')`，只在指定 tag 時跑（ER-42）。
- docstring 第一行寫正規測試 ID，有對應的 T-SEC 時一起寫，例如 `T-ACL-46 / T-SEC-21`。
- U 類測試放在 `W/tests/`，類別繼承 `odoo.tests.BaseCase`。

### 17.3 沙盒指令與 EXPECTED_MIN
- `SLOT=<lane key> dev/sandbox_test.sh [tags]` 包裝 沙盒工具（`$WOOW_SBX`）：copydb 基底 DB → test → 解析測試數 → 比對 EXPECTED_MIN → dropdb。輸出 digest、總數和各檔數。
- 只裝一個模組（ER-42 第 4 點）。各 lane 都不修改 沙盒工具（`$WOOW_SBX`）（ER-47.8）。
- `W/tests/EXPECTED_MIN.json` 由 L11 維護，屬於 always_human。格式：
  ```json
  {"version": 1, "batches": {"core": 0, "share": 0, "portal": 0, "files": 0},
   "files": {"test_core_models.py": 0}}
  ```
  任何一項低於下限，或總數是 0，就失敗。
- 需要真實 HTTP 的 SC 交給 `dev/sc/run_sc.sh` 執行（ER-47.8）：讀 `$WOOW_SBX`，不寫預設值，也不出現 namespace 或主機名稱。

---

## 18. lane → 介面矩陣（P＝提供，C＝使用；L11 依 ER-47.13 唯讀使用全部介面）

| 介面 | L0 | L1 | L2 | L3 | L4 | L5 | L6 | L7 | L8 | L9 | L10 | L11 | L12 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| IF-SKEL | P | C | C | C | C | C | C | C | C | C | ·（間接） | C | C |
| IF-STATIC | P | C | C | C | C | C | C | C | · | · | · | C | C |
| IF-CONST | ·（第 0 天代寫） | C | C | P | C* | C* | C* | C* | C | · | C | C | · |
| IF-SET | · | P | C | C | C | C | C | C | C | · | · | C | C |
| IF-IP | · | P | · | C | · | C | · | · | · | · | · | C | C |
| IF-LOG | · | C | · | P | C | C | C | C | C | · | · | C | · |
| IF-UPL | · | · | P | C | C | · | · | C | ·* | · | · | C | · |
| IF-QUOTA | · | · | P | C | · | · | C | · | · | · | · | C | · |
| IF-NAME | · | · | P | C | C | ·* | · | ·* | · | · | · | C | · |
| IF-LOCK | · | · | P | · | C | C | C* | · | · | · | · | C | · |
| IF-MODEL | · | · | · | P | C | C | C | C | C | · | C | C | · |
| IF-SHARE-SCHEMA | · | · | · | P | · | C | ·* | · | · | · | · | C | · |
| IF-FOLD | · | · | · | P | C | C | · | C | · | · | · | C | · |
| IF-URL | · | · | · | P | · | ·* | · | ·* | C | · | · | C | · |
| IF-GUARD | · | · | · | P | · | · | · | · | · | · | · | C | · |
| IF-UNINST | C | · | · | P | · | · | · | · | · | · | · | C | C* |
| IF-STREAM | · | · | · | · | P | C | · | C | · | · | · | C | · |
| IF-ZIP | · | · | · | · | P | C | · | C | C | · | · | C | · |
| IF-ROUTE-INT | · | · | · | · | P | · | · | · | C | · | · | C | C |
| IF-TOKEN | · | · | · | · | · | P | · | · | · | · | · | C | · |
| IF-SHARE | · | · | · | · | · | P | C | · | C | · | · | C | · |
| IF-ROUTE-SHARE | · | · | · | · | · | P | · | · | · | C | · | C | C |
| IF-MAIL | · | · | · | · | · | P | · | · | C | C | · | C | · |
| IF-TRASH | · | · | · | · | · | · | P | · | C | · | · | C | · |
| IF-CRON | · | · | C* | · | · | C* | P | · | · | · | · | C | · |
| IF-PORTAL | · | · | · | · | · | · | · | P | · | C | · | C | C |
| IF-UI | · | C*（選單） | · | · | · | · | · | · | P | C | · | C | · |
| IF-TEST | · | ·* | ·* | C | C | C | C | C | C | C | C | P | C |
| IF-RB | · | · | · | P（IF-RB-1～5） | · | · | · | · | · | · | P | C | C |
| IF-REL | · | · | · | · | · | · | · | · | · | · | · | C | P |
| PR-01 | P | C | C | C | C | C | C | C | C | · | C | C | · |
| PR-02 | P（骨架） | · | · | · | · | · | · | · | · | · | · | P | · |
| PR-14 | · | · | · | P | · | · | · | · | · | · | C | C | · |

- 標 `*` 的是本文建議補進 lanes.json `depends_on_interfaces` 的依賴（實際會用到，但 lanes.json 沒有列）：
  - L4、L5、L6、L7 加 IF-CONST；
  - L6 加 IF-LOCK（`bounded_wait`）；
  - L5 和 L7 加 IF-NAME、IF-URL（ZIP 檔名、範本裡的網址）；
  - L1 和 L2 加 IF-TEST；
  - L2 和 L5 加 IF-CRON（被 cron 呼叫）；
  - L12 加 IF-UNINST（README 和 runbook）。
- L8 依 ER-47.13 加上 IF-LOG 和 IF-CONST，矩陣已經納入。

---

## 19. 文件之間的衝突和採用的解法（設計勘誤 和 IDS 優先）

| # | 衝突 | 解法 |
|---|---|---|
| 1 | sec §6.1 把 S2 放在 `document_share.py`；ER-44 放在 `share_service.py`，用 `_inherit` 判斷 model | 採 ER-44：S2 在 `models/share_service.py::_resolve_request_access`；`document_share.py`（L3）只放 schema |
| 2 | arch §7.1 寫 `max_content_length`＝callable min(95 MiB, 設定)；§7.2 寫 ×檔數；sec R8 要求 callable；sec R1 要求 route 參數都是字面量；lanes L7 寫 `=streaming.max_upload_body` | 採 ER-03 和 ER-47.2：decorator 寫整數運算式 `95 * 1024 * 1024`；check_routes R1 和 R8 接受「值等於 99,614,720 的整數常數運算式」，或名稱是 `max_upload_body` 的 callable；`streaming.max_upload_body(controller=None)` 保留給測試比對（**IC-R03**） |
| 3 | arch §11、§16 的內部上限預設 50 MiB | 改成 95（ER-03） |
| 4 | arch §7.3 的落地頁要 `session.touch()`；sec R11 要求「沒有任何 request.env」 | 採 ER-22：可以 render、可以用 csrf_token，但不碰 woow model、不 touch、不寫 session、不記 log |
| 5 | sec R11 允許 `_legacy_gone()`；R3 和 R6 有 legacy 例外 | 刪除（ER-27） |
| 6 | target 寫 `/close` 的 sudo 是 0；R11 卻要求 public 函式的第一個陳述句是 `_share_entry` | `_share_entry(resolve=False)` 不呼叫 S2，也不碰 DB（**IC-R04**） |
| 7 | arch §10.2 用 Python 的 `hash()` 產生鎖的鍵 | 改用 sha256 的 `advisory_key`（ER-24） |
| 8 | ER-24 寫 `SET LOCAL lock_timeout` 再 `TO DEFAULT`；ER-47.3 改成 `set_config(...)`，沒寫還原 | 用 ER-47.3 的 set_config，再用同一個語句還原原本的值，四個語句都列進 Q4（**IC-R08**） |
| 9 | arch §6.4 的 Q1 寫 `ON CONFLICT (索引名稱)`、`count = count + 1`；§3.7 的索引沒有 COALESCE(ip_hash) | 採 ER-28 的 SQL，逐字照抄 |
| 10 | ER-47.4 規定 Q3 只有兩個位置；arch §3.2、§3.5 卻要求 access 和 tag 的運算式唯一索引 | 這兩個（以及 attachment_id、token_hash）改在 `init()` 用 `odoo.tools.sql.create_unique_index` 建立（`tools/sql.py` L563），不寫 raw SQL；Q3 維持兩個位置（**IC-R07**） |
| 11 | arch §13.4 的 purge 用 `SKIP LOCKED`，但 Q1～Q6 沒有這一條 SQL；IDS 的 T-TRASH-10 又要求「跳過被鎖住的列」 | 不新增 SQL。改用 `locks.bounded_wait(cr, ms)`：在每一筆的 savepoint 裡短暫設定 lock_timeout，逾時就跳過。語句沿用 Q4 的 set_config（**IC-R09**，L2 提供、L6 使用） |
| 12 | arch §14 有 22 種事件、含 legacy channel | 改成 21 種、5 個 channel（ER-37） |
| 13 | arch §1.3、§4.1 和 L10 細部規格 §4.1 的註腳都說 admin 透過 groups.xml 的 users 欄位加入 | 改用 `data/admin_assignment.xml`，是 noupdate 的 `<function>`（ER-38、ER-45.5、PR-14） |
| 14 | arch §25 的 HD-10，以及 sec R2b-01「HD-10 第二階段才適用」 | 採 ER-15 的改寫版，並顯示提醒 |
| 15 | arch §8.7 的寄信用 `force_send=True` | 改成 `force_send=False`，HD-08 改寫（ER-35） |
| 16 | arch §19 的 uninstall 順序沒有 legal hold | 先檢查 legal hold（M-71），再檢查確認碼（M-70）（ER-30） |
| 17 | arch §3.4、§10.1、§11 和 target 的 note 都有 `policy_flag` 和 `legacy_unverified` | 刪除；`_served_mimetype` 照 ER-36 |
| 18 | 舊 ic 寫 checksum 用 sha1、`exp` 是 float | 改成 `_compute_checksum`，`exp` 用整數 epoch（ER-39） |
| 19 | lanes L10 只有 4 種 SKIP 原因，files 少了 `run.sh` 和 `tests/test_pure.py`；L10 細部規格 的 T-RB 編號和 IDS 不同；L10 細部規格 §1 說 test_pure 是 T-RB-01 | 依 ER-45 第 1、2 點：9 種原因；補上兩個檔案；編號一律用 IDS（test_pure＝T-RB-12；「重跑 T-RB-04」改成 T-RB-02～05） |
| 20 | sec §7 R12 需要 `tools/routes.lock.json`，但 lanes L0 的 files 沒有列 | 歸 L0（在 `tools/**` 底下，屬於 always_human）；第 0 天是空清單，各 lane 新增 route 時自己更新（**IC-R12**） |
| 21 | lanes L0 的 files 有「舊模組 target 退役」 | 依 ER-46 第 2 點：改由 L12 協調**沒有參與實作的主 session** 執行，L0 不得開啟那個檔案 |
| 22 | lanes L8 的依賴缺 IF-LOG 和 IF-CONST；L11 的依賴只列一部分 | 依 ER-47.13 補上；L11 改成全部介面（唯讀） |
| 23 | sec 只寫到 CA-01～33 | 擴充成 CA-01～35（ER-41） |
| 24 | arch §4.6 沒有說文件管理員和 su 的 `_woow_has_rank` 怎麼算（他們不在 eff 集合裡，但 rule R-F4 全開） | su 和文件管理員一律成立（仍然套用公司 rule），`woow_user_rank` 是 4（**IC-R05**） |
| 25 | arch §12 說「mailto 可以設定開啟」，但 §16 沒有對應的設定鍵 | MVP 固定 `allow_mailto=False`，不新增設定鍵（**IC-R13**） |
| 26 | arch §3.8 寫 `_validate(name, data, channel)`、`quota._check(add_bytes)`；lanes 寫 `_validate(name, data)`；ER-24 要求在 checksum 之後才取配額鎖 | `_validate(name, data)`，通道依 env.user（arch §11 本身也這樣寫）；`_check(checksum, size)` |
| 27 | arch §8.5 寫 `_scope_domain(scope_env)`；lanes 寫 `_scope_documents` | 兩者都提供：`_scope_domain()` 回傳 domain，`_scope_documents(scope_env)` 回傳 recordset |
| 28 | lanes L7 說有 6 個 entry point；ER-47.5 說有 5 個 decorated 函式 | 兩者一致：5 個 decorated 函式，加上 `_prepare_home_portal_values`；「文件頁加下載」共用一個函式，用固定的路徑結尾分辨（**IC-R14**） |
| 29 | 分享 ZIP 要不要計入 max_downloads，沒有任何文件定義 | 計入：在 `lock('share:<id>')` 之內記一筆 `share_download`（detail 帶 zip=1）；取鎖順序是 zip → share（**IC-R11**） |
| 30 | sec §8 的 check_public_methods 只允許覆寫 create、write、unlink、copy；但 `set_values`、`message_post`、`message_subscribe`、`init`、精靈的 `default_get` 都必須覆寫 | public_methods.txt 增加 [override] 區段（§4.9），規則是「必須呼叫 super，或直接丟錯」（**IC-R10**） |
| 31 | lanes L4 寫 M-65 用在 page 和 share 兩種 kind，但 M-65 的文案是給分享頁的 | 兩種 kind 都用 M-65 的文案，只有標頭不同；如果要改文案，送 L4 和 L9 |
| 32 | 舊 ic §10 說 CA-23 不成立就「擋發版」 | 改成 L2 從 INLINE_WHITELIST 拿掉 `application/pdf`（ER-47.14） |
| 33 | arch §1.2 寫 private repo 和 README.rst | 改成 public repo 和 README.md（ER-01） |
| 34 | IDS §2.2 寫 test_static_checks 涵蓋 T-STAT-01～13、15～25 | 依 ER-47.6：01～13、16～25；T-STAT-15 放在 test_core_assumptions（PR-02） |
| 35 | sec §10 列 28 支 regression_tests | 改成 36 支（ER-23） |
| 36 | ER-07 和 sec §11 的 sensitive_code 是 23 項 | 改成 24 項，加上 share_service.py；always_human 也加上同一個檔案（ER-44 第 3 點） |
| 37 | constants.py 歸 L3，但其他 lane 第 0 天就要用到上限值 | L0 第 0 天依 §5 寫進最終值，之後由 L3 擁有（**IC-R02**） |
| 38 | ER-32 的 stub 表沒有涵蓋 tokens、share_service、守衛、hook 等檔案 | §3 補上，全部 fail closed（**IC-R01**） |
| 39 | 落地頁載入整包 frontend assets 會帶入 inline script，和 T-SHARE-42 衝突 | 另外建一個 bundle `woow_document_management.assets_share`（§2.2）（**IC-R06**） |

---

## 20. 舊 ic 章節號碼（設計勘誤 ER-43 引用）→ 本文

| ER-43 引用的舊位置 | 本文 |
|---|---|
| §1 第 3、4 點（ER-32） | §1.1、§3 |
| §2 表 | §1.3、§18（snapshot 列已刪除） |
| §3.1 | §1.3 |
| §3.2 | §2.2 |
| §4.2（checksum） | §7.1 |
| §4.3（EVENTS、CHANNELS） | §5、§8.4 |
| §4.4（Response） | §9.1 |
| §4.6（session 授權、落地頁） | §10.3 |
| §4.8 | 刪除（搬遷） |
| §5 | §1.2 R-RESP |
| §6 | §4.3、§4.4 |
| §7 | §17.2、§17.3 |
| §8 | §1.4、§16 |
| §9 | DEC-02、03、05、06 作廢；DEC-15、18 依 ER-02 改寫（不再有搬遷模組的介面） |
| §10（Q-A） | 作廢；改成本文 §19 |


---

## 21. 審查勘誤（contract verifier，2026-11-07；綁定，優先於本文前面各節）

以下 B1～B7 的取代文字直接取代所指章節的原文；實作以本節為準。
## B1：S2 的 domain 改成三元 tuple

§4.3 表格 S2 列的「數量」欄，換成：
> 2：(a) `self.sudo().search([(<'token_hash'｜'id'>, '=', v)], limit=1)`（單一三元 tuple，第一個元素是字面量 `'token_hash'` 或 `'id'`，運算子是字面量 `'='`）；(b) `share_sudo.with_user(share_sudo.create_uid).with_context(allowed_company_ids=[...])`

§10.2 `_resolve_request_access` docstring 的前三行，換成：
```
share_sudo = self.sudo().search([('token_hash', '=', h)], limit=1)   # sudo-ok: S2
    （或 [('id', '=', int(share_id))]）
```

## B2：superuser 建立者
§1.2 R-SU 表格下方那一段，換成：
> `with_user(u)` 產生的 env 是 `su=False`（CA-33），**只有 u 是 base.user_root（uid == SUPERUSER_ID）時例外**：`api.Environment.__new__` 會強制 su=True（`api.py` L572）。另外，`with_user(空 recordset)` 會直接回傳原 recordset（`models.py` L6306–6307）。因此：(1) S2-b 只能在確認 `share_sudo` 非空之後才執行；(2) `_validate_for_access(scope_env)` 的第一步是 `if scope_env is None or scope_env.su: return False`；(3) `_check_create_rights` 和 `_issue` 在 `self.env.su` 時也要執行，建立者是 base.user_root 就丟 UserError(M-52)。測試一律用 `env_as(role)` 建立分享。ER-06 的驗證 env 使用一般使用者，不受影響。

§10.2 `_validate_for_access` docstring，換成：
> self 是 share_sudo。第一步：`scope_env is None or scope_env.su` → False（建立者是 superuser 的分享一律無效）。接著依序檢查 arch §8.5 第 1～5、7 步，任何一步不成立就回傳 False，不 raise。

`check_sudo` 的別名清單有 `SUPERUSER_ID`，所以實作要用 `scope_env.su` 判斷，不要寫 `SUPERUSER_ID`。

## B3：except 的順序
§1.2 R-RESP 第二點後面加一句：
> Odoo 18 的 `AccessError`、`MissingError`、`ValidationError` 都繼承 `UserError`（`exceptions.py` L64、L85、L94），`WoowUploadRejected` 也是。所以 except 一律依這個順序：`LockBusy` → `(AccessError, MissingError)` → `WoowUploadRejected` → `(ValidationError, UserError)`。`AccessError`／`MissingError` 一律回 `uniform_404`，不帶任何訊息。

§9.1 `create_batch` docstring 的例外三行，換成：
```
- LockBusy → rollback savepoint → service_busy()；
- AccessError、MissingError（先接，因為它們是 UserError 的子類別）→ rollback savepoint → uniform_404('page')；
- WoowUploadRejected、ValidationError、其他 UserError → rollback savepoint → 400
  {"ok": false, "message": <M-15>, "errors": [{"index": i, "code": code, "message": <M-10>}]}；
```

`serve` docstring 最後一句改成：
> 只接 `AccessError`、`MissingError` → return uniform_404(…)；不接一般的 UserError。

## B4：分享 ZIP 的掛鉤
§9.1 `zip_response` 簽名改成：
```python
def zip_response(documents, *, folders=None, root_folder=None,
                 channel: Literal['backend', 'portal', 'share'],
                 share_id: int | None = None, ip_hash: str | None = None,
                 before_build: 'Callable[[], Response | None] | None' = None,
                 before_stream: 'Callable[[ZipStats], Response | None] | None' = None) -> Response:
    """…（原文保留）…
    順序：取 zip 鎖（zip:s 或 zip:u）→ 取 slot → 檢查候選文件筆數及讀取權限
    → before_build() 在任何檔案讀取、壓縮與暫存檔建立之前取得 share 鎖並檢查下載次數；
      回傳 Response 時直接拒絕，None 才繼續 → 用 stored file_size 檢查上限並 build_zip
    → before_stream(actual_stats) 記錄成功下載事件 → 串流。
    channel == 'share' 時兩個 callback 都必填（沒有就 AssertionError）；
    share 鎖持有到交易結束，故建檔期間沒有另一個請求可超額。"""
```

§10.3 `share_zip` docstring，換成：
> `zip_response(docs, channel='share', share_id=…, ip_hash=…, before_build=preflight, before_stream=record)`。preflight() 依序做：`lock(cr, 'share:<id>')`（rank 2，在 zip 鎖之後）→ 在 scope_env 計算 share_download 的 ok 筆數，和 max_downloads 比對，超過就 return uniform_404('share')；record(actual_stats) 在建檔成功後記 `share_download`，detail 是 `{"zip": 1, "files": stats.files, "bytes": stats.bytes}`。LockBusy 由 zip_response 轉成 service_busy()。取鎖順序：zip:s → slot → share（IC-R11）。

§19 #29 解法末尾加：
> 由 `zip_response(before_build=…, before_stream=…)` 實作（L4 提供掛鉤，L5 提供 callback）。

## B5：單一擁有權
§3 最後一列「controllers（L4、L5、L7）」的 stub 行為，換成：
> 只有類別，沒有任何 route。`tools/routes.lock.json` 在第 0 天由 L0 依本文 §9.3、§10.3、§12 寫好全部 16 列的最終內容，之後各 lane 不修改。check_routes R12 在非 `--g1` 模式檢查「抽到的每個 route 都和 lock 檔的某一列完全相同」（子集）；`--g1` 模式檢查兩者相等。route 要改，就送契約變更請求給 L0（IC-R12）。

§19 #20 的解法，換成上面這段的最後兩句。

§1.1 第 2 點，換成：
> 要改本文的任何簽名、xmlid、常數或設定鍵，都要對擁有的 lane 發「契約變更請求」。`docs/CONTRACTS.md` 只有 L0 能改：擁有 lane 合併時，在 PR 描述的 `### CONTRACTS` 區塊交出變更紀錄文字，由 L0 合併進去。

§7.1 INLINE_WHITELIST 的註解，換成：
> `# CA-23 不成立時，由 L2 拿掉 'application/pdf'，變更紀錄交 L0 記入 docs/CONTRACTS.md（ER-47.14）`

lanes L11 deliverable「記入 docs/CONTRACTS.md 變更請求」照這個方式解讀。

## B6：補上 stub
§3 表格補以下幾列，或擴充原列：
| `controllers/streaming.py`（L4） | 原列再加：`apply_common_headers(response, kind)` 回傳加上最終標頭的 response（純函式，第 0 天就寫最終邏輯，不標 WOOW-STUB 也可以） | ER-32 |
| `models/share_service.py`（L5） | 原列再加：`_maintenance_run()` 第一個陳述句是 `if not self.env.su: raise AccessError`，之後不做事、回傳 None；`_issue` 丟 NotImplementedError；`download_count` 的 compute 給 0 | IC-R01 |
| `models/document.py`（L3） | 原列再加：module-level `SOURCE_CTX_KEY = 'woow_doc_source'`、`_SOURCE_SENTINEL = object()` 用最終值 | IC-R01 |
| `services/zip_builder.py`（L4） | 原列再加：`limits_from_settings(settings)` 用最終邏輯（純函式） | IC-R01 |

## B7：`_woow_has_rank` 的語意
§8.1 `_woow_has_rank` docstring，換成：
> ensure_one。su → `bool(self.exists())`。文件管理員 → `bool(self.search_count([('id', '=', self.id)]))`（search 會套用公司 rule 和 active；`exists()` 是不套 rule 的 raw SELECT，`models.py` L5971–5973，所以不能拿來判斷文件管理員）。內部使用者 → `bool(self.search_count([('id','=',self.id)] + E(n)))`。非內部使用者 → False。

§19 #24 的解法，換成：
> su 一律成立；文件管理員只要在自己的公司範圍內 search 得到就成立（`woow_user_rank` 是 4）；其他公司的 id 回傳 False（**IC-R05**）。

§14.2 IF-RB-4 的「語意照 §8.1」不用改。改動後，L3 依 §14.2 末段通知 L10。

### 21.8 不擋但要一併處理的觀察（建議，照做）
【不擋的觀察，建議一起修】
1. §7.4 `lock()` 用 `cr.savepoint(flush=False)`，另外讀取並還原 lock_timeout。ER-47.3 寫的是 `cr.savepoint()`。這是相容的強化，但要在 §19 #8 註明偏離 ER-47.3 的字面寫法。`bounded_wait` 包住的是 ORM unlink，應該用預設的 flushing savepoint，rollback 時才會清掉 ORM cache。
2. 配額：同一批上傳的前幾個檔案在 `_check` 時還沒寫入，`_usage_bytes` 看不到，整批可能超過配額。建議在 §8.1 寫明 document.create 逐筆「validate → create → flush」，或在 `_check` 加 pending_bytes 參數。
3. IDS §2.2 寫 `static/tests/tours/**` 歸 L8（T-CA-21 的 tour 例外歸 L11）。ic 和 lanes 再把 tours/share/** 切給 L5（T-SHARE-40）、tours/test_suites/** 切給 L11（T-ESC tour）。這和 IDS 的位階不一致，建議在 §19 補一列說明，並送 IDS 勘誤。
4. L3 的 `_log_event` 正常路徑需要 L1 的 `_share_keys`（ER-32 規定第 0 天的 stub 丟 NotImplementedError），所以 L3 要等 L1 合併後 rebase 才能把測試跑綠。合併順序本來就是 L1 先，不擋，但建議第 0 天先放 `hmac_ip`／`truncate_ip` 的最終純函式。



---

# 第 2 部分：代號表（淨化版）

## 1. Lane

### 1.1 正規 lane key

| Lx | key | 名稱 | 和 lanes.json 的差異 |
|---|---|---|---|
| L0 | `ci-sweep` | repo 骨架、第 0 天介面 stub、static tools、CI、sweep 設定、私有 target、推送前檢查 | 加 stub（ER-32）、推送前檢查（ER-01）；target 改 56 類加 review_units（ER-07） |
| L1 | `settings-ip` | 設定（S3）和真實客戶 IP | 內部上限 95 MiB（ER-03） |
| L2 | `upload-quota` | 上傳政策、配額、naming、mime_map、**advisory lock**（`services/locks.py`） | 加 locks.py 和 test_locks.py（ER-24） |
| L3 | `core-models-acl` | 核心 model、權限、附件守衛、mail_thread_guard（R2b-01）、log 和 share 的 schema、Q1、uninstall_hook | 加 R2b-01 和 test_mail_thread_guard.py（ER-20）、legal hold 拒絕解除安裝（ER-30）、admin noupdate（ER-38）、移除 `policy_flag` |
| L4 | `internal-routes-zip` | streaming（Response 契約）、內部路由、ZIP、原生路由測試 | 加 test_native_routes.py、`service_busy`（ER-24、ER-29、ER-36） |
| L5 | `share` | 分享連結、session 授權、密碼、限流、寄信 | **由 `share-legacy` 改名**，拿掉 legacy_urls 的部分 |
| L6 | `trash-audit-cron` | 回收桶、法律保留、稽核行為、所有 cron | 加 poison record（R2b-06）、資料夾還原需要管理（ER-13） |
| L7 | `portal` | `/my/docs` 瀏覽、下載、原子上傳、麵包屑折疊 | 請求上限固定 95 MiB（ER-03） |
| L8 | `backend-ui` | 後台 UI、附件總覽、加入資料夾、搬移和通知精靈 | 加 HD-10 提醒（ER-15）；通知信改成佇列寄送 |
| L9 | `i18n-zh-tw` | zh_TW 翻譯和用語 | 刪除 LU 的 i18n；「G-5」改稱 T-I18N-01／02 |
| L10 | `site-rebuild`（repo 外） | 一次性重建腳本（repo 外的重建腳本） | **取代 `migration`**（ER-06） |
| L11 | `test-suites` | 共用 fixture、獨立的安全測試、CA-01～CA-35、覆蓋率、SC 執行器 | 加 CA-34、CA-35、`dev/coverage.sh`（ER-09） |
| L12 | `paas-release` | 打包、平台關卡（G-IP、G-COOKIE、G-CACHE）、runbook、發版紀錄、殘留檢查 | 刪除搬遷時序；加 G-CACHE（ER-10）、殘留檢查（ER-34）；「G-7」改名 G-SIM |

舊 key 的別名只用在換算，新文件不准再寫：
- `share-legacy` → `share`
- `migration` → `site-rebuild`（repo 外）

### 1.2 舊編號 → 正規 lane

| 出處 | 舊代號 | 正規 lane |
|---|---|---|
| arch §24（以及 sec §1 的 lane 欄） | L1 | L3 `core-models-acl` |
| 同上 | L2 | L1 `settings-ip` ＋ L2 `upload-quota`（log 歸 L3） |
| 同上 | L3 | L8 `backend-ui` |
| 同上 | L4 | L5 `share` |
| 同上 | L5 | L7 `portal` |
| 同上 | L6 | L4 `internal-routes-zip` |
| 同上 | L7 | L6 `trash-audit-cron` |
| 同上 | L8 | L10 `site-rebuild`（repo 外）（範圍改成 ER-06；legacy 部分刪除） |
| 同上 | L9 | L0 `ci-sweep` ＋ L11 `test-suites` |
| 同上 | L10 | L12 `paas-release` |
| lanes.json | L0～L12 | 號碼相同；L5 和 L10 改名，見 §1.1 |

### 1.3 合併順序（依賴的意思是「合併」，不是「開工」；ER-32）
1. L0 合併
2. L1
3. L2
4. L3
5. L4
6. L5、L6、L7（可以並行）
7. L8
8. L10
9. L9
10. L11 收尾
11. L12

L0 合併之後，L1～L8 和 L11 就可以開工。L10 要等 L3 合併後才能在沙盒跑測試。

### 1.4 各 lane 擁有的檔案（只列和 lanes.json、ic §3.1 不同的地方；`W/`＝`woow_document_management/`）

| lane | 新增 | 刪除 |
|---|---|---|
| L0 | `ops/prepush-check.sh`（repo 外）、`tools/sql_allowlist.yaml`（Q1～Q6）、`tools/cleanroom_forbidden.txt`（只放 ER-33 的內容） | LU 和 MS 的 manifest 與 `__init__.py`；plat 的 sh target 退役改成 G3 交付 |
| L1 | — | — |
| L2 | `W/services/locks.py`、`W/tests/test_locks.py` | — |
| L3 | `W/tests/test_mail_thread_guard.py`、`W/data/admin_assignment.xml`、`dev/sc/sc_uninstall.py` | `policy_flag` 欄位 |
| L4 | `W/tests/test_native_routes.py` | — |
| L5 | — | `LU/**`、T-LG 系列 |
| L6 | `dev/sc/sc_purge_skip_locked.py` | — |
| L7 | — | — |
| L8 | — | — |
| L9 | — | `LU/i18n/*` |
| L10 | 重建腳本（repo 外） | `MS/**` 全部 |
| L11 | `dev/coverage.sh`、`dev/sc/sc_ca*.py`（CA-06、23、24、28）、`W/tests/EXPECTED_MIN.json` | `dev/sandbox_test.sh` 裡三個模組的部分 |
| L12 | `ops/residual-check/**`（repo 外）、G-CACHE 的 ruleset 和套用腳本、`dev/sc/sc_share_max_downloads_race.py` 的執行紀錄 | 舊的搬遷 runbook；`cloud-addons.list` 裡的 legacy_urls |

`dev/sc/` 底下每支腳本的擁有者，以上表和 §2.2 為準。這是「`dev/**` 歸 L11」的例外。

---

## 2. 測試 ID 的規則（唯一一套）

1. **格式**：`T-<FAMILY>-<nn>`。family 以測試計畫（tp §4）為底。
2. **一個 ID 對一個檔案**。一個 ID 可以涵蓋同一個檔案裡的幾個 method。**檔案決定擁有的 lane**（§2.2）。
3. **T-SEC-nn 不是測試**，是 sweep 的控制清單（56 類 × 控制措施）。每一項都對到一個以上的正規測試 ID（§4）。G1 以 T-SEC 清單驗收。
4. **docstring 第一行**寫正規 ID，有對應的 T-SEC 時一起寫，例如 `T-ACL-46 / T-SEC-21`。
5. **method 名稱**：照 tp §4；本文 §2.3 列出的例外和新增項目以本文為準。
6. **SC**（沙盒腳本）放在 `dev/sc/`；**U**（純 Python）放在 `W/tests/`，類別繼承 `odoo.tests.BaseCase`。`services/tests/` 這個目錄不建立。`tools/tests/` 照舊（CI 和本機都可以跑）。

### 2.1 family

| family | 範圍 | 主要檔案 |
|---|---|---|
| T-CA | Odoo core 假設 CA-01～35 | test_core_assumptions.py；SC 放在 dev/sc/ |
| T-STAT | static tools、repo 衛生 | tools/tests/test_static_checks.py；CI |
| T-ACL | 權限、欄位 groups、RPC 暴露、附件守衛、mail 守衛、名稱折疊 | 見 §2.2 |
| T-MC | 多公司 | test_multicompany.py（T-MC-04 在 test_share_routes.py） |
| T-SET | 設定、URL 政策、客戶 IP | test_settings_bounds.py、test_url_policy.py、test_client_ip.py |
| T-ESC | 輸出跳脫 | test_escaping.py |
| T-SHARE | 分享的 model、路由、信件 | test_share_token.py、test_share_routes.py、test_mail_share.py |
| T-PORTAL | portal | test_portal_routes.py、test_portal_upload.py |
| T-ZIP | ZIP、下載回應、原生路由、Response 契約 | test_zip_builder.py、test_internal_routes.py、test_native_routes.py |
| T-UPL | 上傳政策、naming、配額、鎖、內部上傳路由 | test_upload_policy.py、test_naming.py、test_quota.py、test_locks.py、test_core_models.py、test_internal_routes.py |
| T-TRASH | 回收桶、purge、cron | test_trash_lifecycle.py、test_cron.py |
| T-AUD | 稽核紀錄 | test_audit_log.py |
| T-UNINST | 解除安裝 | test_uninstall.py；dev/sc/sc_uninstall.py |
| T-ATT | 附件總覽、加入資料夾、來源單據 | test_attachment_overview.py、test_add_attachment.py |
| T-BUI | 後台 view、精靈、tour、HD-10 提醒（新增） | test_backend_views.py、test_backend_wizards.py、static/tests/tours |
| T-I18N | 翻譯 | test_i18n_complete.py |
| T-PERF | 效能 | test_perf.py |
| T-RB | 一次性重建腳本（新增） | 重建腳本（repo 外） |
| T-REL | 打包和發版檢查 | plat、cloud-odoo、docs/release |
| T-E2E | 端到端和平台關卡 | SC 和人工 |
| T-SEC-P | sweep 流程 | bundle 副本、G2 |

### 2.2 檔案 → lane → ID

| 檔案（都在 `W/tests/`，另外註明的除外） | lane | 正規 ID |
|---|---|---|
| `tools/tests/test_static_checks.py` | L0 | T-STAT-01～13、16～25 |
| test_settings_bounds.py | L1 | T-SET-01～08 |
| test_client_ip.py | L1 | T-SET-20～25 |
| test_upload_policy.py | L2 | T-UPL-01、02、04～09 |
| test_naming.py | L2 | T-UPL-03、18、19 |
| test_quota.py | L2 | T-UPL-11～14 |
| test_locks.py | L2 | T-UPL-30、31 |
| `dev/sc/sc_quota_contention.py` | L2 | T-UPL-32 |
| test_core_models.py | L3 | T-ACL-02～06、09、10、17～20、28、48、49、54、57；T-UPL-10、15、16 |
| test_security_acl.py | L3 | T-ACL-07、08、12～16、51、52、56、58 |
| test_multicompany.py | L3 | T-MC-01～03、05～08 |
| test_attachment_guard.py | L3 | T-ACL-30～39、55 |
| test_mail_thread_guard.py | L3 | T-ACL-46、47、53 |
| test_uninstall.py | L3 | T-UNINST-01、02、04、08 |
| `dev/sc/sc_uninstall.py` | L3 | T-UNINST-03、05 |
| test_display_name_folding.py | L3 | T-ACL-40～43 |
| test_folding_service.py | L3 | T-ACL-44 |
| test_url_policy.py | L3 | T-SET-10～14 |
| test_zip_builder.py | L4 | T-ZIP-01、02、04～07、13、27 |
| test_internal_routes.py | L4 | T-ZIP-03、08～12、14、20～25；T-UPL-20～23 |
| test_native_routes.py | L4 | T-ZIP-26 |
| test_share_token.py | L5 | T-SHARE-01～11 |
| test_share_routes.py | L5 | T-SHARE-20～31、33～43；T-MC-04 |
| test_mail_share.py | L5 | T-SHARE-12、50～57 |
| `dev/sc/sc_share_max_downloads_race.py`、`dev/sc/sc_denied_count_real_http.py`、`dev/sc/sc_source_limit_two_links.py`、`dev/sc/sc_denied_sources_together.py`、`dev/sc/sc_denied_many_sources.py`、`dev/sc/sc_denied_one_source_together.py` | L5 | T-SHARE-32、44、45、46、47、48 |
| test_trash_lifecycle.py | L6 | T-TRASH-01～08、13 |
| test_cron.py | L6 | T-TRASH-09、11、12 |
| `dev/sc/sc_purge_skip_locked.py` | L6 | T-TRASH-10 |
| test_audit_log.py | L6 | T-AUD-01～09 |
| test_portal_routes.py | L7 | T-PORTAL-01～09、15～18 |
| test_portal_upload.py | L7 | T-PORTAL-10～14 |
| test_attachment_overview.py | L8 | T-ATT-01～03 |
| test_add_attachment.py | L8 | T-ATT-04～09 |
| test_backend_views.py | L8 | T-BUI-01～04、08 |
| test_backend_wizards.py | L8 | T-BUI-05、06 |
| `static/tests/tours/**` | L8 | T-BUI-07（另外 T-CA-21 的 tour 歸 L11） |
| test_i18n_complete.py | L9 | T-I18N-01～05 |
| 重建腳本（repo 外） | L10 | T-RB-01～11 |
| test_record_rules.py | L11 | T-ACL-01、11、21、22、29、45 |
| test_rpc_exposure.py | L11 | T-ACL-23～27、50 |
| test_core_assumptions.py；`dev/sc/sc_ca*.py` | L11 | T-CA-01～35（SC：06、23、24、28） |
| test_escaping.py | L11 | T-ESC-01～03 |
| test_perf.py | L11 | T-PERF-01～04 |
| `dev/coverage.sh` | L11 | T-REL-05 的量測 |
| plat、cloud-odoo、docs/release | L12 | T-REL-01～04、06；T-E2E-01～05 |
| bundle 副本、G2 | L0（P1、P3～P5）、L12（P2 協調） | T-SEC-P1～P5 |

## 4. T-SEC（sweep 控制清單）→ 正規測試 ID

| T-SEC | 正規 ID | lane |
|---|---|---|
| 01 | T-ACL-01 | L11 |
| 02 | T-ACL-51 | L3 |
| 03 | T-ACL-21 | L11 |
| 04 | T-ACL-22 | L11 |
| 05 | T-ACL-11 | L11 |
| 06 | T-MC-03 | L3 |
| 07 | T-MC-04 | L5 |
| 08 | T-ACL-12 | L3 |
| 09 | T-ACL-13 | L3 |
| 10 | T-ACL-12 | L3 |
| 11 | T-ACL-14 | L3 |
| 12 | T-ACL-15 | L3 |
| 13 | T-ACL-23 | L11 |
| 14 | T-ACL-24 | L11 |
| 15 | T-ACL-25 | L11 |
| 16 | T-ACL-30、31 | L3 |
| 17 | T-ACL-32 | L3 |
| 18 | T-ACL-33、34 | L3 |
| 19 | T-ACL-35、36、37 | L3 |
| 20 | T-ACL-38 | L3 |
| 21 | T-ACL-46 | L3 |
| 22 | T-ACL-47 | L3 |
| 23 | T-ACL-52（TC）、T-PORTAL-09（HC） | L3、L7 |
| 24 | T-SHARE-01、02 | L5 |
| 25 | T-SHARE-04 | L5 |
| 26 | T-SHARE-03 | L5 |
| 27 | T-SHARE-24 | L5 |
| 28 | T-SHARE-25 | L5 |
| 29 | T-SHARE-26 | L5 |
| 30 | T-SHARE-22、30、31 | L5 |
| 31 | T-SHARE-23 | L5 |
| 32 | T-SHARE-23、30 | L5 |
| 33 | T-SHARE-28、29 | L5 |
| 34 | T-SHARE-10 | L5 |
| 35 | T-SHARE-31、32 | L5 |
| 36 | T-SHARE-35、36 | L5 |
| 37 | T-SHARE-30 | L5 |
| 38 | T-SHARE-34、38 | L5 |
| 39 | T-SHARE-33 | L5 |
| 40 | T-SHARE-20、41 | L5 |
| 41 | T-SHARE-37 | L5 |
| 42 | T-SHARE-35、T-PORTAL-18 | L5、L7 |
| 43 | T-SHARE-50 | L5 |
| 44 | T-SHARE-51、52 | L5 |
| 45 | T-SHARE-53 | L5 |
| 46 | T-SHARE-54、12 | L5 |
| 47 | T-SHARE-55 | L5 |
| 48 | T-ACL-40～44、T-ACL-45、T-PORTAL-05 | L3、L11、L7 |
| 49 | T-PORTAL-11、03 | L7 |
| 50 | T-ESC-01 | L11 |
| 51 | T-SET-10、11、14 | L3 |
| 52 | T-UPL-03、04、05 | L2 |
| 53 | T-UPL-08、09 | L2 |
| 54 | T-UPL-11、12、13 | L2 |
| 55 | T-ZIP-01、08、04、07 | L4 |
| 56 | T-ZIP-12 | L4 |
| 57 | T-TRASH-03、01 | L6 |
| 58 | T-TRASH-07、13 | L6 |
| 59 | T-TRASH-06 | L6 |
| 60 | T-AUD-02、03、07、04 | L6 |
| 61 | T-SET-01、02、03 | L1 |
| 62 | T-SET-20～24 | L1 |
| 63 | T-UNINST-01～05 | L3 |
| 64 | T-PERF-01、03 | L11 |
| 65 | T-ATT-06、05、04 | L8 |
| 66 | T-ATT-01 | L8 |
| 67 | T-CA-01～35 | L11 |
| 68 | T-ZIP-26、T-PORTAL-08、T-CA-30 | L4、L7、L11 |
| 69～74 | 廢止 | — |
| 75 | T-STAT-01～13、16～25 | L0 |
| 76 | T-SHARE-27、28、T-ACL-17 | L5、L3 |
| 77 | T-SHARE-42、T-STAT-05 | L5、L0 |
| 78 | T-UPL-30 | L2 |
| 79 | T-ACL-02 | L3 |
| 80 | T-ACL-52 | L3 |
| 81（新） | T-SHARE-43、44，T-ZIP-27，T-STAT-23（Response 不 raise，ER-29） | L5、L4、L0 |
| 82（新） | T-UNINST-08（有 legal hold 就拒絕解除安裝，ER-30） | L3 |
| 83（新） | T-ACL-53（portal hash／pid／token fallback，ER-20） | L3 |
| 84（新） | T-SHARE-40、21（貼上完整連結；query string 不採用，ER-12） | L5 |
| 85（新） | T-TRASH-05（資料夾還原需要管理，ER-13） | L6 |
| 86（新） | T-UPL-30、31，T-STAT-22（鎖的鍵、lock_timeout、503，ER-24） | L2、L0 |
| 87（新） | T-RB-01～10（重建只給檢視、冪等、只印 id，ER-06） | L10 |
| 88（新） | T-SHARE-54（佇列寄送後沒有 token，ER-35） | L5 |
| 89（新） | T-ACL-54（admin noupdate，ER-38） | L3 |
| 90（新） | T-E2E-04（G-CACHE 紀錄，ER-10） | L12 |

sweep 類別對 T-SEC 的對照是 sec §3，套用 設計勘誤 ER-23 第 3 點的修改。

---

## 5. R2b → lane

| R2b | 嚴重度 | 正規 lane | 測試 | 設計勘誤 |
|---|---|---|---|---|
| R2b-01 | high | L3 `core-models-acl` | T-ACL-46、47、53（T-SEC-21、22、83） | ER-20 |
| R2b-02 | medium | L5 `share`（static 規則在 L0） | T-SHARE-20、41，T-STAT-21（T-SEC-40） | ER-22 |
| R2b-03 | medium | L5 `share`（static 規則在 L0） | T-SHARE-34、38，T-STAT-21（T-SEC-38） | ER-39 |
| R2b-04 | medium | L4 `internal-routes-zip`（殘餘）；HD-12 文字由 L3 提供；CA-35 由 L11 撰寫 | T-ZIP-26，T-CA-35（T-SEC-68） | ER-36（搬遷部分不適用） |
| R2b-05 | medium | L2 `upload-quota`（使用者是 L4、L5） | T-UPL-13、30、31，T-STAT-22（T-SEC-54、78、86） | ER-24 |
| R2b-06 | low～medium | L6 `trash-audit-cron` | T-TRASH-13（T-SEC-58） | ER-32 |
| R2b-07 | low | 不適用 | — | ER-25 |
| R2b-08 | medium | 不適用 | — | ER-26 |
| R2b-09 | needs_validation | L4（T-ZIP-26）＋ L11（T-CA-30）＋ L12（G-CACHE） | T-ZIP-26，T-CA-30，T-E2E-04 | ER-10 |
| R2b-10 | low | L0 `ci-sweep` | T-STAT-20（T-SEC-75） | ER-23 |

---


---

# 第 3 部分：變更紀錄

| 日期 | lane | 變更 |
|---|---|---|
| 2026-11-07 | L0 ci-sweep | 第一版公開契約：收錄介面契約全文（含 §21 B1～B7 審查勘誤，B1～B7 直接取代所指章節）與代號表 §1、§2.1～2.2、§4、§5 的淨化版。 |
| 2026-11-07 | L0 ci-sweep | **ER-02：取消兩個模組。** 舊網址相容模組（`woow_document_management_legacy_urls`）和一次性搬遷模組（`woow_document_management_migrate_sh`）連同它們的路由（含 410 頁）、model、腳本、測試和 sweep 設定全部取消；repo 只有一個 addon `woow_document_management`。舊路由沒有任何模組註冊，回 404 就是最終行為。資料的一次性重建改由 repo 外的 L10 site-rebuild 腳本處理（只給「檢視」）。 |
| 2026-11-07 | L0 ci-sweep | 第 0 天骨架：manifest（§2.2）、全部 `__init__.py`（§2.3）、§1.3 每個檔案的佔位、§3 與 §21 B6 的 stub（標 `# WOOW-STUB`，fail closed）；`services/constants.py` 依 §5 寫最終值（IC-R02）；`security/groups.xml` 定稿（PR-01）。 |
| 2026-11-07 | L0 ci-sweep | `tools/routes.lock.json` 第 0 天就是 16 列最終內容（§21 B5）；`check_routes` 在非 `--g1` 模式檢查「每個 route 都在 lock 檔」，`--g1` 檢查兩者相等並要求 17 筆 entry point。 |
| 2026-11-07 | L0 ci-sweep | §21.8 第 4 點照做：`services/client_ip.py` 的 `truncate_ip`、`hmac_ip` 第 0 天就是最終純函式（其餘函式仍是 stub）。 |
| 2026-11-07 | L0 ci-sweep | §21.8 第 1 點的註記：§7.4 `lock()` 用 `cr.savepoint(flush=False)` 並讀取、還原 `lock_timeout`，是 ER-47.3 字面寫法（`cr.savepoint()`）的相容強化；`bounded_wait` 包住 ORM unlink，要用預設會 flush 的 savepoint。 |
| 2026-11-07 | L0 ci-sweep | §21.8 第 2 點的註記（待 L2／L3 決定）：同一批上傳的前幾個檔案在 `_check` 時尚未寫入，`_usage_bytes` 看不到；建議 `document.create` 逐筆 validate → create → flush，或 `_check` 加 pending_bytes 參數。 |
| 2026-11-07 | L0 ci-sweep | §21.8 第 3 點的註記：`static/tests/tours/share/**` 歸 L5、`static/tests/tours/test_suites/**` 歸 L11、其他 tours 歸 L8，以 §1.3 為準。 |
| 2026-11-07 | L0 ci-sweep | static tools 的實作約定：`tools/sql_allowlist.yaml` 的 Q6 允許守衛檔（`models/ir_attachment.py`）內任何函式（契約只寫到檔案）；HD-07 的註記寫在 portal 那一列 ACL 的 `id` 或 `name` 欄（CSV 沒有註解語法）；`tools/security_matrix.yaml` 的 rule domain 以「解析後的 Python 運算式」比對，空白和引號不影響；`quota._cron_check_warn` 的 stub 在 su 檢查之後丟 NotImplementedError（§3 原文寫「丟 AccessError」，非 su 呼叫仍是 AccessError）。 |
| 2026-11-09 | G4 hardening | 附件建立前先合併 `default_get` 對應的目標與存取欄位，再執行模組 guard 與 Odoo core 權限檢查。分享 ZIP 的 folder 範圍以分享根目錄為起點，文件清單分享使用平鋪檔名；相同名稱由 ZIP builder 去重。拒絕事件保留各 share 的稽核歸屬，限流計數 Q13 則跨 share ID 以 IP 摘要聚合。文件搬移只留模組稽核事件，不將 `folder_id` 寫入 chatter tracking。 |
| 2026-11-10 | G4 修正 | **R-M0**：`mail.message` 全域 ir.rule，`user.share` 為真時排除 `woow.document` 與 `woow.document.folder`，portal 不再能經 ORM／RPC 讀取這兩個 model 的 chatter（DEC-11）；內部使用者不變。`tools/security_matrix.yaml` 登錄 R-M0，`check_security_matrix` 解析 core model 的 `model_id` ref。 |
| 2026-11-10 | G4 修正 | **audit log 保留期例外**：`_cron_log_retention` 不清除 `state=active`、未過期且 `max_downloads>0` 的分享的 `share_download`／ok 列（它們是下載計數）；分享過期或撤銷後依 `log_retention_days` 清除。`log_retention_days` 合法範圍 30～3650 不變。 |
| 2026-11-10 | G4 修正 | **分享精靈密碼**：`password` 改為不儲存的 compute 欄位，create / write 立即雜湊到 `password_hash`；`_issue` 接受精靈已雜湊的值，不重複 hash；`init()` 以固定 DDL（Q3）刪除舊 schema 的明文欄。 |
| 2026-11-10 | G4 修正 | **改名政策**：`WoowDocument.write` 只改 `name` 時呼叫 `upload_policy._validate_rename`：所有文件套 `sanitize_filename`；binary 文件另要求副檔名在允許清單、不在禁用清單且與儲存的 MIME 一致；url 文件只套名稱規則。名稱未變或寫入其他欄位不檢查，既有資料不批次更名。 |
| 2026-11-11 | G4 修正 | **拒絕紀錄容量**：每分鐘新增的拒絕紀錄列最多 `MAX_DENIED_ROWS_PER_MIN`（1024）列，由 `woow.document.denied.budget` 的分鐘列配給（Q14）；占用的是新的來源計數列與新的 denied bucket，已存在的列不占用。占容量的 denied 事件（`share_access`、`rate_limited`）的 bucket 以（事件、來源、分鐘）為鍵、不寫 share_id：有效連結沒有授權與不存在的 id 寫進同一個 bucket、用同樣的容量，所以剩下的容量也不透露分享是否存在。`share_unlock_failed` 的計數列與 bucket 不占用（`UNBUDGETED_EVENTS`）：比對密碼前必須持有有效連結與 session 授權，每個連結每小時最多計 50 次（`SHARE_UNLOCK_FAIL_PER_HOUR`），列數以連結數為上限。`_take_slot` 先以呼叫端交易的快照讀本分鐘的列：已達上限就直接回 False，不開第二條連線、不寫也不鎖該列（同一分鐘內槽數只增不減，舊快照只會少算）；未滿時才在自己的短交易（READ COMMITTED）裡以呼叫端交易的分鐘 upsert（`DO UPDATE … WHERE count < MAX`，count 就是已用的槽數、最多 MAX）後立即 commit：請求交易不會在同一個分鐘列上相遇，取槽不會 serialization_failure，也不在請求期間持有該列；請求之後 rollback 或重試時已取的槽不退回（只會少用，不會超用）。registry test mode 下改用共用測試交易的 cursor。分享入口不檢查容量（移除 Q15），有效 token、已授權 session、密碼嘗試都不受容量影響（arch §15、HD-06）。`_cron_log_retention` 一併刪除兩天前的分鐘計數列。 |
| 2026-11-11 | G4 修正 | **來源限流計數**：來源＝`client_ip.source_key`（IPv4 原位址；IPv6 取 /64；IPv4-mapped 取其 IPv4），denied 列的 `ip_hash` 與計數列都用來源的 HMAC。`_count_denied_across_shares` 讀 `woow.document.denied.counter` 的（事件、來源、分鐘）列：不存在時在本交易 `INSERT … ON CONFLICT DO NOTHING` 建立 count 0 的列（同來源的並行請求等它，再以 serialization_failure 重試），存在時 `SELECT … FOR UPDATE` 鎖定（`INSERT … DO NOTHING` 不鎖快照看得到的列，只有鎖定讀取會在他交易於本快照後改過此列時失敗）（Q13）；新列需要容量而沒有時刪除並回傳 None。`_peek_denied_across_shares` 只讀快照不鎖。`_log_event` 記 `share_access`、`share_unlock_failed` 的 denied 時在同一交易內把計數加一；新 bucket 沒有容量時不寫 bucket、回傳 0，但已存在的計數列照樣加一，限流判斷不依賴容量。限流判斷（取代 §8.4「限流判斷」與 §10.3 `_share_entry` 第一點）：`_denied` 只依鎖定的來源計數回應：新來源沒有容量 → 429 且不寫任何列；已達 `denied_rate_per_min` → 記一筆不帶 share_id 的 `rate_limited`、429；否則記 denied、404。回應與容量都不依分享是否存在：被拒絕的連結請求一律不帶 share_id 記錄（`_log_event` 對 `UNBUDGETED_EVENTS` 以外的 denied 事件清掉 share_id），只有計入的錯誤密碼（`share_unlock_failed`）保留 share_id，供同分享同來源冷卻與同分享每小時上限計數；回應也不依 bucket 是否寫入，容量用盡時也不成為分享存在與否的判斷依據。 |
| 2026-11-11 | G4 修正 | **密碼解鎖與分享列屏障**：`share_unlock` 先以請求快照判斷來源跨分享上限、同分享同來源冷卻、同分享每小時上限（舊快照只會少算，據此拒絕必定正確）：來源上限已滿走 `_denied`（`rate_limited`、429），其餘 429，皆不取 `share:<id>` 鎖、不寫 share 列。要比對時才 `_lock_password_attempts`（`share:<id>` advisory 鎖＋`SELECT … FOR NO KEY UPDATE`，只鎖不寫列版本，Q12）、鎖定來源計數列，再判斷一次上限，然後比對密碼。錯誤密碼先 `_count_password_failure`（`UPDATE … SET write_date = write_date` 寫新的列版本），再記 `share_unlock_failed`、回 404；正確密碼移除本次建立、count 仍為 0 的來源計數列，回 303。所以只有計入的失敗會寫 share 列：快照早於它的嘗試在列鎖得到 serialization_failure 並重試，而沒有計入失敗的嘗試（正確密碼、被拒絕的嘗試）不會讓擁有者的撤銷或編輯 serialization_failure。下載同理：`_download_slot_available` 先以快照拒絕已用完的連結（不鎖、不寫），其餘取 `share:<id>` 鎖與列鎖後重新計數；`_record_download` 只對有次數限制的連結寫列版本，再記 `share_download`。鎖順序：`share:<id>` → share 列 → 來源計數列 → 事件 bucket；容量槽在獨立交易取得。SC：T-SHARE-44 先在複製的 DB 安裝模組，期望 10 次 404、1 次 429、share_access bucket 10、`rate_limited` 1、來源計數 10；T-SHARE-45（`dev/sc/sc_source_limit_two_links.py`）：同一來源兩個用戶端同時對兩個連結猜密碼，比對過的猜測恰為上限 3，之後同來源的正確密碼也回 429；T-SHARE-46（`dev/sc/sc_denied_sources_together.py`）：12 個來源（信任的迴環 proxy 加 `X-Real-IP`）同時送被拒絕的開啟請求，第一輪同時送一次正確密碼，伺服器紀錄不得有並行錯誤重試，拒絕全為 404、解鎖 303、容量恰為每個新計數列與新 bucket 各一格。真 HTTP 的 SC 以資料庫時鐘對齊分鐘，跨分鐘的那一輪清空計數後重跑一次。 |
| 2026-11-11 | G4 修正 | **User-Agent 清理**（補充 §8.4 `user_agent` 一點）：`_log_event` 寫入前先移除 user agent 的控制字元（`str.isprintable()` 為假的字元，含 NUL），再截斷到 256 字元（`USER_AGENT_SIZE`），ok 與 denied 兩條路都套用。PostgreSQL 的 text 不能存 NUL：原樣寫入會讓請求 500 並整筆 rollback，錯誤密碼與被拒絕的請求都不會計入冷卻與限流，而側交易已取的容量槽不退回。 |
| 2026-11-12 | 審查修正 | **附件與串流**：`ir.attachment.write` 依寫入後的值判斷，帶 `res_field`、`public` 或 `access_token` 的附件不能分兩步改掛到本模組 model；`message_post` 等經 `_process_attachments_for_post` 加入討論的附件不能是欄位附件（Q17）；`ir.binary._record_to_stream` 只串流文件記錄的那一個 binary 附件，`_find_record` 對本模組記錄拒絕 `access_token`（`/web/content` 404、`/web/image` placeholder，不再是 public 快取）。**mail**：`message_new` 一律拒絕；`mail.thread._message_route_process` 路由到文件或資料夾時只接受 su 或系統管理員；portal 只能移除自己的 follower；follower 在讀不到記錄後不再收到通知（Q18，HD-10 的無帳號聯絡人不變）；資料夾的 record_name、信件主旨、活動名稱只用資料夾自己的名稱。**名稱與排序**：同層同名回 M-02（Q19，不論另一個資料夾是否看得到；DB 唯一索引保留）；別的 model 依指向資料夾的 many2one 排序或分組時，非文件管理員改依資料夾 id；文件的 display_name 依呼叫者的 record rule 折疊為「Restricted document」；guard mixin 的 `_search` 與 `_read_group_groupby` 檢查欄位 groups（many2many、one2many 也算），`/my/docs` 的直接分享改走 `_woow_portal_direct_documents`（Q20）。**配額**：一般上傳一律計入檔案大小，只有加入資料夾精靈抵扣已存的相同內容；百分比只給文件管理員。**分享信**：佇列中的分享信，其 mail.message 副本的 body 清空，連結只留在 mail.mail。**回收桶**：已隨上層刪除的記錄略過；cron 每批最多刪 PURGE_BATCH 筆記錄，大型子樹分批完成。**稽核**：`access_change` 每個資料夾一列、只列該資料夾的成員列 id；detail 的 id 清單最多 500 個。**上傳**：ZIP64 end record 與 central directory 的實際標頭數也套用 4096 筆、1 MiB 的上限，ZipFile 之前就拒絕。**設定**：信任代理與用戶端 IP 標頭加上說明。 |
| 2026-11-12 | 審查修正 | **回收桶與 legal hold**：分批 purge 的每個根資料夾在讀任何 hold 之前先鎖住自己的列（Q21，`SELECT … FOR NO KEY UPDATE`，只鎖不寫列版本）。還原會改寫 `active`，在本批快照之後 commit 的 hold 或還原讓該根 serialization_failure、整個根都不刪（更正：hold 設在個別丟進回收桶的子資料夾或其下時，當時的 `subtree_legal_hold` 不會改寫根，見下一列）；之後才開始的 hold 等這一批 commit。文件的 hold 與刪除是同一列，並行設下的文件 hold 讓刪除 serialization_failure。這兩種錯誤（與並行死結）都只讓該根或該文件算 failed、跳過，整批照常 commit，不再讓 cron 批次失敗。**通知**：follower 讀取權重驗先套公司規則（R-D0、R-F0 沒有 groups，portal 帳號同樣受限），portal 分支另要求 base.group_portal（R-D5、R-F5 是 portal 群組的規則）；只在其他公司的 portal follower 與直接分享對象不再收到該記錄的通知。 |
| 2026-11-12 | 審查修正（第二輪） | **purge 屏障**：`_woow_lock_purge_root`（Q21）改為依 id 順序鎖住根與子樹每個資料夾的列（子樹以 active_test=False 取得），本批快照之後 commit 的 hold、還原或搬移只要改到子樹任一資料夾列，該根就 serialization_failure、整個根都不刪，不再依賴儲存的聚合欄位；`subtree_legal_hold` 與 `has_restricted_descendant` 改以 `with_context(active_test=False).child_ids` 計算，個別丟進回收桶的子資料夾（及其下）的 hold 也會到達根，`init()` 每次安裝與更新時重算。**分享 id 路由**：`_share_entry` 先讀 session 授權，沒有就直接 `_denied`，不存在、已撤銷與有效的 id 查詢數相同。**附件**：`ir.attachment.write` 在所有 env（含 su）都拒絕讓附件寫入後成為本模組記錄的另一筆欄位附件（`message_notify` 指定 model／res_id 也擋）；`_record_to_stream` 一律從文件自己的 `attachment_id` 串流（Q17 `_woow_document_file_row` 取代 `_woow_document_file_is_unique`），殘留的其他欄位附件不再讓文件 404，也不會被送出。 |
| 2026-11-12 | 審查修正（第三輪） | **失敗密碼 bucket 的鍵**：`woow.document.log` 新增 `link_ref`（純整數，不是外鍵）。刪除分享連結時 `share_id` 會被外鍵清成 NULL；舊的唯一索引 `woow_document_log_denied_bucket` 以 `share_id` 為鍵，同一來源在同一分鐘對兩個連結各有一個 `share_unlock_failed` bucket 時，刪掉第二個連結會讓兩列變成相同的鍵而 UniqueViolation：每小時的 `_maintenance_run` 整次回滾（過期標記與失敗信件的清除也跟著消失，cron 連續失敗還會被 Odoo 停用），連結所在資料夾的 purge（cascade）也整批回滾。現在 bucket 的鍵是（事件、來源、`COALESCE(link_ref, 0)`、分鐘）：只有 `UNBUDGETED_EVENTS` 的 denied 列寫入 `link_ref`（等於當時的 `share_id`），其他 denied 事件仍不帶連結（鍵為 0）；`link_ref` 寫入後不再改變、id 也不重用，所以連結被刪後各 bucket 仍各自獨立，留作稽核列（`share_id` 為 NULL、`link_ref` 保留）。唯一索引改名為 `woow_document_log_denied_link_bucket`，ON CONFLICT 推斷式（Q1）與 `_denied_bucket_exists`（Q16）同步改用 `COALESCE(link_ref, 0)`；`_count_share_unlock_failures` 仍以 `share_id` 計數（有索引，存活連結的兩者相同）。`init()` 在舊索引還在時，先把舊版寫下的 denied 列的 `link_ref` 設成各自的 `share_id`（鍵不變、仍唯一），再建新索引並刪舊索引；舊索引的存在就是「尚未搬」的標記，之後的 `init()` 跳過。兩條清理路徑各自隔離單一記錄：`_maintenance_run` 刪舊連結時接住 `IntegrityError`（記一筆只含 id 的 warning，下一輪再試），`_PURGE_SKIPPED` 加入 `IntegrityError`（該記錄算 failed、同樣只記 id，批次其餘照常 commit，`remaining` 不計 failed 的規則不變）。 |
| 2026-11-12 | 審查修正（第三輪） | **取槽的第二條連線**：取槽（`_take_slot`）要借第二條連線，threaded server 在沒有執行緒上限時（`workers = 0` 且沒有 `ODOO_MAX_HTTP_THREADS`），同時有多個新來源的拒絕請求就會超過 `db_maxconn`（預設 64）：先前連線池滿時 `PoolError` 讓請求回 500、先前已 commit 的槽不退回，借走的連線也讓其他請求（包含有效的分享連結）拿不到自己的連線，core 在 `_transactioning` 取游標時同樣丟 `PoolError`（500）。現在：(1) 取不到連線（`psycopg2.pool.PoolError`，或資料庫拒絕連線的 `OperationalError`）視為沒有槽，回 False：新來源當分鐘的第一次拒絕走既有的「容量用完」路徑（429、不寫任何列、計數列釋放），已有計數列與 bucket 的來源照常計數，錯誤密碼不需要槽；只處理「拿不到連線」，取得連線之後的錯誤照常往上丟。(2) 每個行程同時開著的取槽交易最多 `SLOT_CONNECTIONS`＝`max(1, db_maxconn // 32)` 個（預設 2），其餘的最多等 `SLOT_WAIT`（0.5 秒），逾時同樣是沒有槽：取槽最多占連線池的三十二分之一，請求自己的連線不會被它們搶走，同時處理的請求不超過 `db_maxconn` 減 `SLOT_CONNECTIONS` 時不會因取槽而拿不到連線；取槽的速率本來就受每分鐘容量（1,024）限制，額滿後的拒絕從快照直接判斷、不再借連線。prefork 每個 worker 一次只處理一個請求，不會等。`ODOO_MAX_HTTP_THREADS` 設成整數時直接當執行緒上限，設成非整數值（例如 `auto`）時取 `(db_maxconn - max_cron_threads) // 2`；SECURITY.md 新增部署說明。SC：T-SHARE-47（`dev/sc/sc_denied_many_sources.py`）以 60 個來源同時對 threaded server 送被拒絕的請求：答案只能是 404 或 429、不可有 5xx、伺服器紀錄不得有錯誤追蹤或重試，來源計數列要等於答 404 的拒絕數。 |
| 2026-11-12 | 審查修正（第四輪） | **分享檔案路由的文件 id 範圍**：`share_file` 的 `document_id` 來自網址，超過資料庫 integer 範圍（2**31-1）時，`('id', '=', x)` 的搜尋會讓 PostgreSQL 回 `NumericValueOutOfRange`（core 把值轉成文字，由資料庫轉成 integer），請求回 500、不計入來源計數也不受限流，持有授權的人可以無限重複。現在範圍之外（含 0）的 id 視為分享範圍裡找不到：不查詢，和其他找不到的 id 一樣走 `_denied`，回 404、計入來源的 `share_access`，達到 `denied_rate_per_min` 後回 429；內部與 portal 路由以 `browse(…).exists()` 取記錄，不受影響。**圖片像素上限**：`upload_policy._validate` 開啟圖片時，Pillow 拿宣告的尺寸和自己的上限比對：超過兩倍上限丟 `DecompressionBombError`（是 `Exception`，不是 `OSError` 或 `ValueError`），超過一倍只警告（警告被設成錯誤時同樣是例外）。先前這兩者都沒有被接住，一個 IHDR 宣告 200000×200000 的 PNG 標頭（約 45 位元組）就讓上傳（內部、portal，經 `create_batch`、`document.create`、`document.write`）回 500。現在兩者都對應 `WoowUploadRejected('pixels')`（400 JSON），像素數的比對移到 `with` 之後；讀不懂的圖片仍是 `content`。**文件登錄**：SC 執行紀錄、L5 檔案清單與 runner／lane／案例表補上 T-SHARE-47（`dev/sc/sc_denied_many_sources.py`）。 |
| 2026-11-12 | 審查修正（第五輪） | **聊天欄位**：`message_ids`、`website_message_ids`、`has_message` 在 `woow.document` 與 `woow.document.folder` 加 `groups='base.group_user'`（auto_join 的 one2many 在 domain 以子查詢查 `mail_message`，不經 R-M0；portal 能測試自己讀得到的記錄上內部 log note 的文字）。**精靈列**：六個精靈 model 各加一條 `create_uid = user.id` 的 ir.rule（R-W1～R-W6，`tools/security_matrix.yaml` 登錄）：core 對 transient model 沒有擁有者規則，精靈的 action 以呼叫者的權限依列內的值執行，別人不能讀或改管理員正要確認的那一列（管理員也看不到別人的列）。**資料夾**：`write` 的 `company_id` 只有文件管理員能改（非管理員改成別家公司或空值 → AccessError，寫入目前的值不算變更），`create` 的 `company_id` 為空也只有文件管理員能建（T-MC-05 `test_shared_company_false_manager_only`）；把資料夾 `parent_id` 清空（搬到最上層）要過和建立根資料夾一樣的政策（M-24，`allow_root_create` 或文件管理員），先於名稱檢查。**沒有公司的記錄**：`_log_event` 的 `company_id` 接受 False（記成空公司，R-L0 讓各公司管理員都讀得到），先前 `ValueError` 讓上傳、丟進回收桶與 purge 在沒有公司的資料夾上失敗，而 purge cron 沒有接住，整批回滾、之後每天卡住；purge 的每一筆現在接住任何例外（只記 id 與例外類別），並實作 R2b-06 的管理員提醒（`POISON_ALERT_DAYS`：本次失敗且保留期結束已超過該天數的記錄，每位文件管理員每天最多一個 activity，只寫筆數）。**ZIP**：`naming.Deduper` 一次一個名稱、每個（主檔名、副檔名）記住下一個數字，`build_zip` 同一層 n 個檔案不再做 n×n 次 `_fit_segment`（輸出名稱逐字相同，`dedupe` 的簽章與結果不變）；`zip_response` 不在列出文件時建立串流，`ZipEntry.open_stream` 在寫到該項目時才讀（`_lazy_stream`），檔案存在資料庫時大小上限不再要等所有檔案載入記憶體之後才判斷。**分享**：建立者已被刪除（`create_uid` 為空）的連結不再解析（`with_user` 空 recordset 保留 superuser env，請求的使用者會被當成建立者）、`_validate_for_access` 依 B2 的第一步檢查 `scope_env`；公開的 `share_open`／`share_access`／`share_download` 以請求的環境記錄（訪客，不是建立者），`_record_download` 新增必填參數 `log_env`；`share_close` 只在確實移除授權時才寫 session；`_scope_documents` 以 `_woow_filter_rank` 一次查出資料夾的 manage 權限，不再每份文件查一次。**設定**：`res.config.settings.default_get` 對非系統管理員拿掉 `woow_doc_*` 欄位。**上傳**：`document.create` 對非 su 套用每次請求的檔案數上限（`max_files_per_upload`）；PNG、JPEG、GIF 的標頭結構在 Pillow 開啟前先計數（`IMAGE_MAX_STRUCTURES`、`IMAGE_MAX_JUNK`，超過 → `'content'`）；`/web/content`、`/web/image` 對文件的 `datas` 固定使用上傳政策的類型，`?mimetype=` 無效，非 inline 類型一律 attachment，CSP 固定含 `sandbox`，文件欄位附件的 id 路由 → MissingError。**配額**：一次呼叫的檔案在全部驗證之後一起檢查（`_check_many`），已 commit 的用量以獨立連線在取得配額鎖之後讀（取代 `UPDATE ir_config_parameter` 屏障，Q10 移除：它讓並行上傳 serialization_failure，core 睡眠後重試，一個帳號連續上傳就讓其他人的上傳變慢或回 500），同一個交易的多次呼叫共用 `QuotaLedger`；每個帳號有自己的 `quota:u:<uid>` 鎖（rank 3，先於 `quota`），一個帳號同一時間只有一個上傳交易；`create_batch` 讀每個上傳之前先 `seek(0)`。**拒絕計數**：`ATOMIC_EVENTS`（`share_access`）的拒絕改走 `_refuse`：來源計數列（Q22）與 audit bucket（Q23）各由一條語句在自己的 READ COMMITTED 短交易裡更新，立即 commit，不再在請求的 Repeatable Read 交易裡鎖列，所以同一來源的並行請求不互相等待、不 serialization_failure、不被睡眠重試，計入的恰好是上限筆；新列在同一個短交易取容量槽，沒有槽就不建立，槽數等於寫入的列數；取不到連線時所有這類拒絕回 429。錯誤密碼（`share_unlock_failed`，T-SHARE-45）仍在請求交易裡以鎖定的來源計數列與 share 列屏障序列化。SC：T-SHARE-48（`dev/sc/sc_denied_one_source_together.py`）、T-UPL-32（`dev/sc/sc_quota_contention.py`），各自對舊程式碼失敗（重試 33 次、HTTP 500）。 |
| 2026-11-12 | 審查修正（第六輪） | **ZIP 命名**：第五輪的 `Deduper` 以原始的（主檔名、副檔名）記住下一個數字，但 `_fit_segment` 會把長的多位元組主檔名裁短，不同的主檔名因此有同樣的 ` (2)`、` (3)`… 候選，每個新的主檔名都要重掃別人取走的號碼（2000 個「124 個雙位元組字母加兩個不同字元」的名稱各兩次，約 70 萬次 `_fit_segment`、12 秒；只有大小寫不同的 2000 個長名稱，約 200 萬次）。現在記住的鍵是（數字的位數、為該位數的數字裁短後的主檔名、副檔名），後兩者以 `casefold()` 比對；數字每多一位，主檔名就少一個位元組，所以只從 10 起才裁成一樣的名稱也共用同一個計數。每個名稱只多做名稱本身那一次與它試的候選（約 2 次／名稱，四組刁鑽的名稱都在 3 次／名稱之內；沙盒裡 2000 個名稱的 `build_zip`：刁鑽的雙位元組名稱 12.5 秒→0.2 秒，只有大小寫不同的長名稱 36.9 秒→0.3 秒，一般名稱 0.04 秒）；輸出與第一個版本逐字相同（250 回合的隨機名稱：sharp s、Kelvin 符號、帶點的大寫 I、字尾 sigma、共同的長前綴、剛好在裁切點的主檔名、容不下兩位數的副檔名所造成的拒絕，再加上四組縮小的名稱）。**PNG chunk**：`_png_structure_ok` 只限制 chunk 的個數（4096），而且只走到第一個 IDAT。Pillow 會解壓每個 `iCCP`（每個最多 1 MiB，沒有累計上限）與壓縮文字（累計 64 MiB）；4000 個約 1 KB 的 `iCCP`（約 4 MB，在上傳大小之內）讓 `policy._validate` 在沙盒花 13 秒才接受。放在影像資料之後的同樣 chunk 逃得過這個檢查，改由 Odoo 儲存附件時的 `load()` 付代價（`ir.attachment` 的後處理為了讀方向而載入 PNG）：200 個 chunk 的檔案建立 `woow.document` 要 0.7 秒。現在 `_png_structure_ok` 走過整個檔案到 IEND：非影像資料的 chunk 最多 256 個（`PNG_MAX_CHUNKS`）、`iCCP` 1 個（`PNG_MAX_PROFILES`）、`zTXt` 加 `iTXt` 16 個（`PNG_MAX_COMPRESSED_TEXTS`），影像資料的 chunk（`IDAT`、`fdAT`、`fcTL`）超過 4096 個之後平均至少 256 位元組（`PNG_FREE_IMAGE_CHUNKS`、`PNG_IMAGE_CHUNK_BYTES`；成千上萬個空的 `IDAT` 讓函式庫逐一讀取），超過 → `'content'`，不呼叫 `Image.open`（同樣的檔案在 0.01 秒內被拒絕，chunk 放在影像資料之前或之後都一樣）；`tEXt` 的成本等於它的大小，只受 chunk 總數限制。**測試的獨立性**：`TestPurgeRacesWithLegalHold._purge_batch` 不再送出 `_woow_alert_stuck_trash`（競態測試的記錄都過了保留期很久，真的送出會 commit 一個提醒 activity 與它的訊息、追蹤者、通知和 bus 列，沒有任何清理，同一個資料庫上之後的 `test_items_that_stay_undeletable_for_days_alert_the_administrators` 會失敗 `1 != 0`），清理之後並檢查資料庫沒有多出提醒；提醒測試一開始先在自己的交易內刪掉資料庫裡已有的提醒（同一天已有提醒時 purge 不會再建，只比較前後的數量看不出來）。**文件**：T-SHARE-48（`dev/sc/sc_denied_one_source_together.py`）與 T-UPL-32（`dev/sc/sc_quota_contention.py`）補進擁有表、SC 執行紀錄欄位和案例表；§4.4 的 SQL 表補上 Q7～Q9、Q11～Q14、Q16，並拿掉讓 Q22、Q23 脫離表格的空行。 |
