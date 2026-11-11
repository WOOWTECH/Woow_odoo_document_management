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
| L2 upload-quota | `W/models/upload_policy.py`、`W/models/quota.py`、`W/services/naming.py`、`W/services/mime_map.py`、`W/services/locks.py`、`W/tests/test_upload_policy.py`、`test_naming.py`、`test_quota.py`、`test_locks.py` |
| L3 core-models-acl | `W/models/guard_mixin.py`、`document_folder.py`、`folder_access.py`、`document.py`、`document_tag.py`、`document_share.py`、`document_log.py`、`ir_attachment.py`、`mail_thread_guard.py`、`W/hooks.py`、`W/services/constants.py`、`folding.py`、`url_policy.py`、`W/security/ir.model.access.csv`、`rules.xml`、`W/data/admin_assignment.xml`、`W/views/safe_url_templates.xml`、`W/tests/test_core_models.py`、`test_security_acl.py`、`test_multicompany.py`、`test_attachment_guard.py`、`test_mail_thread_guard.py`、`test_uninstall.py`、`test_display_name_folding.py`、`test_folding_service.py`、`test_url_policy.py`、`dev/sc/sc_uninstall.py` |
| L4 internal-routes-zip | `W/controllers/streaming.py`、`W/controllers/main.py`、`W/services/zip_builder.py`、`W/models/zip_actions.py`、`W/views/common_templates.xml`、`W/tests/test_zip_builder.py`、`test_internal_routes.py`、`test_native_routes.py` |
| L5 share | `W/models/share_service.py`、`W/services/tokens.py`、`W/wizards/share_wizard.py`、`W/views/wizards/share_wizard_views.xml`、`W/views/share_views.xml`、`W/views/share_templates.xml`、`W/controllers/share.py`、`W/static/src/share/**`、`W/static/src/share_result/**`、`W/static/tests/tours/share/**`、`W/data/mail_templates.xml`、`W/security/share_access.xml`、`W/tests/test_share_token.py`、`test_share_routes.py`、`test_mail_share.py`、`dev/sc/sc_share_max_downloads_race.py`、`dev/sc/sc_denied_count_real_http.py`、`dev/sc/sc_source_limit_two_links.py`、`dev/sc/sc_denied_sources_together.py`、`dev/sc/sc_http_runtime.py` |
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
- `models/__init__.py`：`guard_mixin, settings, upload_policy, quota, document_tag, document_folder, folder_access, document, document_share, document_log, ir_attachment, mail_thread_guard, res_config_settings, share_service, trash_actions, cron_jobs, zip_actions, backend_actions`。
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
| Q1 | `models/document_log.py::_log_event` | ER-28 第 2 點的 SQL，**逐字照抄** |
| Q2 | `models/quota.py::_usage_bytes` | arch §6.4 Q2 |
| Q3 | `models/document_folder.py::init`（名稱唯一索引）、`models/document_log.py::init`（denied bucket，ER-28 第 1 點） | 只准固定字串的 DDL（ER-47.4） |
| Q4 | `services/locks.py`（`try_lock`、`lock`、`bounded_wait`） | `SELECT pg_try_advisory_xact_lock(%s)`、`SELECT pg_advisory_xact_lock(%s)`、`SELECT current_setting('lock_timeout')`、`SELECT set_config('lock_timeout', %s, true)` |
| Q5 | `models/quota.py::_bytes_if_new` | arch §6.4 Q5 |
| Q6 | `models/ir_attachment.py`（守衛） | arch §6.4 Q6 |

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
    def _validate(self, name: str, data: bytes) -> ValidatedFile:
        """arch §11 的全部步驟。通道由 env.user 決定，不由呼叫端傳入：
        內部使用者（含 su 的 OdooBot）→ internal；share 使用者 → portal；其他 → WoowUploadRejected('channel')。
        單檔上限＝設定的 MiB × MIB，用實際 len(data) 比對（ER-03）。
        最後一步呼叫 env['woow.document.quota']._check(checksum, size)。"""
    def _served_mimetype(self, doc) -> str:
        """ER-36：回傳 doc.mimetype；text/* 加 '; charset=utf-8'；
        不在 mime_map 的可接受集合裡 → 'application/octet-stream'。"""
    def _inline_allowed(self, mimetype: str) -> bool:
        """去掉參數之後的 base type 在 INLINE_WHITELIST 裡才為真。"""
```

**例外**
- 驗證不通過 → `WoowUploadRejected`。它是 UserError 的子類別，所以 RPC 會顯示訊息，controller 會接住，再 return 400 JSON。
- 配額鎖逾時 → `locks.LockBusy` 原樣往外丟，由 controller 轉成 503。

### 7.2 IF-QUOTA：`W/models/quota.py`
- **提供者**：L2。
- **使用者**：L3（經由 `_validate`）、L6（cron）。

```python
class WoowDocumentQuota(models.AbstractModel):
    _name = 'woow.document.quota'
    def _usage_bytes(self) -> int:                      # Q2  # sql-ok: Q2
    def _bytes_if_new(self, checksum: str, size: int) -> int:   # Q5  # sql-ok: Q5；checksum 已存在就回 0
    def _check(self, checksum: str, size: int) -> None:
        """quota_mb == 0 → 直接 return。
        否則：flush ir.attachment → locks.lock(cr, 'quota')（只在驗證和 checksum 之後才取）
        → 新用量 = _usage_bytes() + _bytes_if_new() → 超過 quota_block_pct → WoowUploadRejected('quota', pct=…)。
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
NAME_RE = re.compile(r'^(zip:u:\d+|zip:slot:[1-4]|zip:s:\d+:[0-9a-f]{16}|share:\d+|quota)$')

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
| L2 配額 | `lock(cr, 'quota')` | `LockBusy` → 由路由回傳 `service_busy()` |

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
  - `in_trash`、`subtree_restored_at`、`has_restricted_descendant`、`subtree_legal_hold`：stored、recursive。
  - `woow_user_rank`：Integer，非 stored，`@api.depends_context('uid')`，值是 0～4。文件管理員和 su 是 4。
  - `woow_has_portal_members`：Boolean，非 stored，條件是 `bool(eff_read_partner_ids)`（ER-15）。
  - `effective_access_lines`：Json。`document_count`、`subfolder_count`：在使用者 env 用 `_read_group` 計算。
  - 舊模組的 `policy_flag` **不存在**（ER-36）。

```python
    @api.model
    def _woow_rank_domain(self, n: int) -> list:
        """E(n)，內容見 arch §4.4。"""
    def _woow_has_rank(self, n: int) -> bool:
        """ensure_one。su 或文件管理員 → bool(self.exists())（仍然套用公司 rule）。
        內部使用者 → bool(search_count([('id','=',self.id)] + E(n)))。非內部使用者 → False。"""
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
        and not settings.allow_root_create: raise AccessError(M-24)。不寄信，不自動加 follower（ER-45.4）。"""
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
                   folder_id: int | None = None, company_id: int | None = None,
                   ip: str | None = None, user_agent: str | None = None,
                   detail: Mapping[str, int | bool | list[int]] | None = None) -> int:
        """S1 加 Q1。
        - event、result、channel 不在 constants 的清單裡 → ValueError（屬於程式錯誤）；
        - detail 的值只能是整數、布林值或整數清單，否則 ValueError；
        - ip 沒有傳入，而且有 http request 時，用 client_ip.request_client_ip 取得；user_agent 截斷到 256；
        - user_id 預設是 env.uid（public 使用者記 None）；company_id 預設是 env.company.id。
        result == 'ok'：self.sudo().create(vals)   # sudo-ok: S1   → 回傳 1。
        result == 'denied'：flush_model() → Q1（ER-28 第 2 點，逐字照抄）  # sql-ok: Q1
                           → invalidate_model() → 回傳 RETURNING count（同一個分鐘桶的累計數）。
        denied 列不寫 document_id 和 folder_id，需要時放進 detail。"""
    def init(self):    # Q3：ER-28 第 1 點的 woow_document_log_denied_bucket  # sql-ok: Q3
```

**限流判斷**：呼叫端拿 `count > settings.denied_rate_per_min` 判斷，成立就 return `too_many_requests()`，並另外記一筆 `rate_limited`（denied）。

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
- 守衛只對非 su 生效：
  - **create**：`res_model in WOOW_MODELS` 而且符合以下任一條件 → AccessError：`res_field` 有值、`public` 為真、帶 `access_token`。
  - **write**：
    - Q6 查出 self 裡哪些是「本模組 model 的欄位附件」。對這些列，寫入 `GUARDED_WRITE_KEYS = ('raw','datas','db_datas','mimetype','type','url','res_model','res_id','res_field','public','access_token','store_fname')` 任何一鍵 → AccessError。
    - 對本模組 model 的 chatter 附件（res_model 用目前使用者的 ORM 讀取），寫 `public`、`access_token`、`res_model`、`res_id`、`res_field` → AccessError。
    - 把 `res_model` 改成本模組 model、同時設定 `res_field` → AccessError。
  - `generate_access_token()` 和 `copy()` 會走 write 或 create，所以一起被擋（core 的 generate_access_token 不是 sudo，`ir_attachment.py` L667–676）。

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
    def _message_update_content(self, *args, **kwargs):
    # 在 folder 上：write() 對非內部使用者一律 AccessError（HD-07）
```

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
        一律回傳 tuple。呼叫端禁止使用 share_sudo.env。"""
    def _validate_for_access(self, scope_env) -> bool:
        """self 是 share_sudo。依序檢查 arch §8.5 第 1～5、7 步，任何一步不成立就回傳 False，不 raise。"""
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
        和 max_downloads 比對 → 記 share_download → serve(doc_in_scope_env, inline=False, channel='share', event=None)。"""
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
    每筆包在 savepoint 和 locks.bounded_wait 裡，失敗的記 purge（denied，detail 只放 id）後跳過。回傳 (done, failed)。"""
```
- 精靈：`woow.document.restore.wizard.action_restore_to(self) -> dict`、`woow.document.purge.wizard.action_confirm_purge(self) -> dict`。兩者的第一個陳述句都是權限檢查。

### 11.2 IF-CRON：`W/models/cron_jobs.py` 和 `W/data/ir_cron.xml`
- **提供者**：L6。
- **使用者**：L2（`_cron_check_warn`）、L5（`_maintenance_run`）、L11、L12。

| xmlid | 方法（在 `woow.document` 上，`@api.model`） | 頻率 | 內容 |
|---|---|---|---|
| `ir_cron_woow_document_purge_trash` | `_cron_purge_trash()` | 每天 | 每批 PURGE_BATCH 筆；`ir.cron._notify_progress(done=, remaining=)`；失敗的不算進 remaining；同一筆連續失敗 POISON_ALERT_DAYS 天，就對管理員建立 activity（R2b-06） |
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
   - SC 執行紀錄：T-SHARE-32、44、45、46（L5），T-TRASH-10（L6），T-UNINST-03、05（L3），sc_ca*（L11）。
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
| `dev/sc/sc_share_max_downloads_race.py`、`dev/sc/sc_denied_count_real_http.py`、`dev/sc/sc_source_limit_two_links.py`、`dev/sc/sc_denied_sources_together.py` | L5 | T-SHARE-32、44、45、46 |
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
