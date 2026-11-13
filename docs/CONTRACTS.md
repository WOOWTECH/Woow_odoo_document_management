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
- 本文的章節號碼沿用 lanes.json 引用的位置：§1.3 檔案表、§2.2 manifest、§3 stub 表、§4.9 public method、§9 IF-STREAM、§10 IF-MAIL、§17 IF-TEST。設計勘誤 ER-43 引用的是舊 ic 的章節號碼，換算見 §20。
- 本文自己做的決議編成 **IC-Rnn**，和 PR-xx、ER-xx 分開。

---

## 1. 通則（每條 lane 都要遵守）

### 1.1 擁有權與變更
1. 一個檔案只有一個擁有 lane（§1.3）。L0 第 0 天寫進 stub，**不算擁有權**（ER-32 第 2 點）。之後只有擁有的 lane 能修改。
2. 要改本文的任何簽名、xmlid、常數或設定鍵，都要對擁有的 lane 發「契約變更請求」。擁有 lane 合併時同步更新 `docs/CONTRACTS.md` 的變更紀錄（由 L0 落實）。
3. `depends_on_interfaces` 只代表合併順序（ER-32 第 3 點）。L0 合併後，L1～L8 和 L11 就可以對著 stub 開工。L10 要等 L3 合併，才能在沙盒跑。
4. 合併順序：L0 → L1 → L2 → L3 → L4 → L6／L7（可以並行）→ L8 → L10 → L9 → L11 收尾 → L12（IDS §1.3）。L5（分享）已於 2026-11-13 移除，編號不重用。

### 1.2 共通執行規則

**R-RESP：拒絕和限流一律 return Response，不准 raise（ER-29）**
- `controllers/main.py`、`portal.py` 的每一條失敗路徑，都要 `return` IF-STREAM 的 `uniform_404('page')`、`service_busy()`、`json_response(...)`，或 `limit_page(...)` 其中之一。
- 路由裡的 `AccessError`、`MissingError`、`UserError`、`ValidationError`、`WoowUploadRejected`、`LockBusy`、`ZipLimitExceeded` 都在 controller 接住，再 return。
- 禁止寫 `raise request.not_found(`、`return request.not_found(`、`raise NotFound`、`raise werkzeug.exceptions.`、`abort(`（T-STAT-23）。理由：`request.not_found()` 回傳的是 werkzeug 的 `NotFound` 例外物件（`http.py` L1892）；而 raise 會讓整個請求 rollback（`service/model.py` L163），同一個請求裡 S1 寫下的稽核紀錄會跟著消失。
- model 層照常 raise（AccessError、UserError 等）。只有 controller 有「轉成 Response」的義務。
- 失敗路徑上不寫任何東西：本模組沒有拒絕紀錄，S1 只記成功的事件（I-11、ER-29 第 4 點）。其他寫入都必須在已經 rollback 的 savepoint 裡。

**R-TX：交易**
- **上傳**：一批檔案共用一個 `with env.cr.savepoint():`，在裡面呼叫 `Document.create([...])`。任何一個檔案失敗，savepoint 就 rollback，然後 return 400 JSON。遇到 `LockBusy` 則 return `service_busy()`（503）。
- **ZIP**：在請求的交易裡建到 `SpooledTemporaryFile`，鎖會一直持有到 commit。串流在 commit 之後才開始（`wrap_file` 加 `call_on_close`）。
- **cron**：每一筆記錄包一個 savepoint，失敗的就跳過（R2b-06）。
- **ER-06 重建腳本**：單一交易，commit 之前先驗證（§14）。

**R-LOCK：鎖（ER-24、ER-47.3）**
- 所有 advisory lock 一律經由 `W/services/locks.py`（IF-LOCK）。其他檔案出現 `pg_advisory`、`pg_try_advisory` 就失敗（T-STAT-22）。
- 取鎖順序：`zip`（rank 1）→ `quota`（2）→ `access`（3）。同一個交易裡先拿了高 rank、又要拿低 rank → `AssertionError`。同 rank 重入可以。
- 鎖都是 transaction 級（`*_xact_lock`），commit 或 rollback 時自動釋放。

**R-SU：su 的行為（ER-45.3、ER-45.4、CA-33）**

| 位置 | `env.su` 為真時 | 非 su 時 |
|---|---|---|
| guard mixin 在 create 補做欄位 groups 檢查 | 略過 | 檢查，沒權限 → AccessError |
| folder.create 的根資料夾政策（M-24） | 略過 | `not parent_id and not 文件管理員 and not allow_root_create` → AccessError |
| ir.attachment 守衛 | 丟 AccessError 的檢查略過（框架的 `Binary` 欄位操作是 sudo，CA-07）。但本模組 model 的列在任何 create 或 write 之後都沒有 `public` 與 `access_token`，這在所有 env 都成立：su 不丟例外，create 與 write 在值或預設值帶了其中一個鍵時（不論值是什麼）把兩者設成 False（§8.6）；欄位附件不能被搬移的檢查（寫入後的目標）在所有 env 都執行 | 檢查，AccessError |
| document.unlink | 允許（cron、hook） | 只有文件管理員，而且不能有 legal_hold |
| `_woow_has_rank`、`_woow_require_rank` | 一律成立 | 依 rule |
| `_cron_*`、`_cron_check_warn` | 必要條件 | 第一個陳述句就丟 AccessError |
| uninstall_hook | env 本來就是 su（CA-28） | — |
| mail_thread_guard | 看 `env.user._is_internal()`（OdooBot 是內部使用者） | 同左 |

`with_user()` 產生的 env 一律是 `su=False`（CA-33）。所以 ER-06 的驗證 env 會套用 rule。

**R-LOG：log 和輸出衛生**
- `_logger` 只記 id。log 字串裡不准出現 token、password、email 或名稱類變數。
- 稽核 `detail` 只放整數、布林值和整數清單。

### 1.3 檔案擁有表（`W/`＝`woow_document_management/`）

| lane | 擁有的檔案 |
|---|---|
| L0 ci-sweep | `.gitignore`、`.gitattributes`、`LICENSE`、`COPYING`、`README.md`、`CLEANROOM.md`、`SECURITY.md`、`docs/CONTRACTS.md`、`.security/sweep.yaml`、`.github/workflows/security.yml`、`.github/gitleaks.toml`、`.github/pull_request_template.md`、`tools/**`（含 `tools/routes.lock.json`，IC-R12）、`W/__manifest__.py`、`W/__init__.py`、`W/{models,controllers,wizards,services}/__init__.py`、`W/security/groups.xml`（PR-01）、推送前檢查腳本與其字串清單（兩者都在 repo 外）、plat 的 target `Woow_odoo_document_management.yaml`、README target 表新增的那一列 |
| L1 settings-ip | `W/models/settings.py`、`W/models/res_config_settings.py`、`W/views/res_config_settings_views.xml`、`W/services/client_ip.py`、`W/tests/test_settings_bounds.py`、`W/tests/test_client_ip.py` |
| L2 upload-quota | `W/models/upload_policy.py`、`W/models/quota.py`、`W/services/naming.py`、`W/services/mime_map.py`、`W/services/locks.py`、`W/tests/test_upload_policy.py`、`test_naming.py`、`test_quota.py`、`test_locks.py`、`dev/sc/sc_quota_contention.py`、`dev/sc/sc_http_runtime.py` |
| L3 core-models-acl | `W/models/guard_mixin.py`、`document_folder.py`、`folder_access.py`、`document.py`、`document_tag.py`、`document_log.py`、`ir_attachment.py`、`ir_binary.py`、`mail_thread_guard.py`、`W/hooks.py`、`W/services/constants.py`、`folding.py`、`url_policy.py`、`W/security/ir.model.access.csv`、`rules.xml`、`W/data/admin_assignment.xml`、`W/views/safe_url_templates.xml`、`W/tests/test_core_models.py`、`test_security_acl.py`、`test_multicompany.py`、`test_attachment_guard.py`、`test_mail_thread_guard.py`、`test_uninstall.py`、`test_display_name_folding.py`、`test_folding_service.py`、`test_url_policy.py`、`dev/sc/sc_uninstall.py` |
| L4 internal-routes-zip | `W/controllers/streaming.py`、`W/controllers/main.py`、`W/services/zip_builder.py`、`W/models/zip_actions.py`、`W/views/common_templates.xml`、`W/tests/test_zip_builder.py`、`test_internal_routes.py`、`test_native_routes.py` |
| L6 trash-audit-cron | `W/models/trash_actions.py`、`W/models/cron_jobs.py`、`W/wizards/restore_wizard.py`、`purge_wizard.py`、`W/views/wizards/restore_wizard_views.xml`、`purge_wizard_views.xml`、`W/security/trash_access.xml`、`W/data/ir_cron.xml`、`W/tests/test_trash_lifecycle.py`、`test_audit_log.py`、`test_cron.py`、`dev/sc/sc_purge_skip_locked.py` |
| L7 portal | `W/controllers/portal.py`、`W/views/portal_templates.xml`、`W/static/src/portal/**`、`W/tests/test_portal_routes.py`、`test_portal_upload.py` |
| L8 backend-ui | `W/views/backend/{folder,document,tag,trash,log,attachment_overview}_views.xml`、`W/views/menus.xml`、`W/views/wizards/{move,add_attachment,notify}_wizard_views.xml`、`W/wizards/{move,add_attachment,notify}_wizard.py`、`W/models/backend_actions.py`、`W/data/mail_templates.xml`、`W/security/backend_access.xml`、`W/static/src/{backend,scss,img}/**`、`W/static/description/**`、`W/static/tests/tours/backend/**`、`W/tests/test_attachment_overview.py`、`test_add_attachment.py`、`test_backend_views.py`、`test_backend_wizards.py` |
| L9 i18n-zh-tw | `W/i18n/woow_document_management.pot`、`W/i18n/zh_TW.po`、`docs/i18n-glossary.md`、`W/tests/test_i18n_complete.py` |
| L10 site-rebuild（repo 外） | 重建腳本與其測試（repo 外，不在公開 repo；ER-45 第 1 點） |
| L11 test-suites | `W/tests/__init__.py`（第 0 天版本由 L0 寫）、`W/tests/common.py`、`W/tests/EXPECTED_MIN.json`、`test_record_rules.py`、`test_core_assumptions.py`、`test_rpc_exposure.py`、`test_escaping.py`、`test_perf.py`、`W/static/tests/tours/test_suites/**`、`dev/README.md`、`dev/sandbox_test.sh`、`dev/coverage.sh`、`dev/sc/run_sc.sh`、`dev/sc/sc_ca{06,23,24,28}.py` |
| L12 paas-release | `docs/release/v18.0.1.0.0.md`；repo 外的 `ops/release/**`、`ops/g-cache/**`、`ops/g-ip/**`、`ops/g-cookie/**`、`ops/residual-check/**`；cloud-odoo 和 plat 的打包與 lint 檔案（lanes L12） |

`dev/sc/` 底下每支腳本的擁有者以上表為準，這是「`dev/**` 歸 L11」的例外（IDS §1.4）。

每個審查修正附上 `W/tests/test_reg_<slug>.py`（歸修正所在的 lane）：它要加進 `W/tests/__init__.py`、`.security/sweep.yaml` 的 `verify.regression_tests` 與 `W/tests/EXPECTED_MIN.json` 的 `files`，`tools/tests/test_static_checks.py` 的 `TestTestFilesAreRegistered` 檢查三處與檔案一致。

### 1.4 交給 L0 的文字（README 段落、SECURITY.md 的 HD）
- HD 文字分工（IDS §6）：
  - L3：HD-01、02、03、04、07、09、10（ER-15 版）、12（ER-36）；
  - HD-06、08、11 已退役（它們描述連結式分享，見 SECURITY.md），編號不重用；
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
| M-65 | L4 | 統一的 404 頁（`uniform_404_page`）；L5 的其餘訊息（M-50～M-56、M-60～M-64、M-66）已隨分享移除，編號不再使用 |
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
    'summary': 'Folders, permissions and portal access for documents',
    'license': 'LGPL-3',
    'author': 'WoowTech',
    'website': 'https://github.com/WOOWTECH/Woow_odoo_document_management',
    'depends': ['mail', 'portal'],
    'data': [
        'security/groups.xml',
        'data/admin_assignment.xml',
        'security/ir.model.access.csv',
        'security/rules.xml',
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
        'views/wizards/move_wizard_views.xml',
        'views/wizards/restore_wizard_views.xml',
        'views/wizards/purge_wizard_views.xml',
        'views/wizards/add_attachment_wizard_views.xml',
        'views/wizards/notify_wizard_views.xml',
        'views/portal_templates.xml',
        'views/menus.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'woow_document_management/static/src/backend/**/*',
            'woow_document_management/static/src/scss/**/*',
        ],
        'web.assets_frontend': [
            'woow_document_management/static/src/portal/**/*',
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

### 2.3 `__init__.py` 的匯入順序（L0 擁有；`tests/__init__.py` 由 L11 擁有）
- `W/__init__.py`：
  ```python
  from . import services, models, wizards, controllers
  from .hooks import uninstall_hook
  ```
- `services/__init__.py`：`constants, naming, mime_map, locks, url_policy, client_ip, folding, zip_builder`。
- `models/__init__.py`：`guard_mixin, settings, upload_policy, quota, document_tag, document_folder, folder_access, document, document_log, ir_attachment, ir_binary, mail_thread_guard, res_config_settings, trash_actions, cron_jobs, zip_actions, backend_actions`。
- `wizards/__init__.py`：`move_wizard, restore_wizard, purge_wizard, add_attachment_wizard, notify_wizard`。
- `controllers/__init__.py`：`streaming, main, portal`。
- `tests/__init__.py`（第 0 天版本）：匯入 ER-23 列出的測試檔（扣掉分享的 3 個，現為 32 個），再加上 `test_perf`。

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
| `models/settings.py`（L1） | `_get_settings()` 回傳 §6.1 預設值組成的 frozen `WoowDocSettings`；`_audit_ip_key()` 丟 NotImplementedError；`_read_params()` 回傳 `{}` | ER-32 |
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
| `models/ir_attachment.py`（L3） | 非 su 時：create 的 vals 裡 `res_model` 屬於 `WOOW_MODELS` → AccessError；write 的 vals 只要含 `GUARDED_WRITE_KEYS` 任何一鍵 → AccessError（不分 model） | IC-R01 |
| `models/mail_thread_guard.py`（L3） | `_get_thread_with_access` 對非內部使用者回傳 `self.browse()`，對內部使用者照 core 判斷；`message_post`、`message_subscribe`、`_message_update_content` 對非內部使用者丟 AccessError | IC-R01 |
| `hooks.py`（L3） | `uninstall_hook(env)` 丟 UserError（M-70），一律拒絕解除安裝 | IC-R01 |
| `services/folding.py`（L3） | 函式都回傳「全部折疊」：`display_name` 回傳受限標籤；`breadcrumb` 回傳 `[(None, ELLIPSIS)]`；`roots` 和 `children_of` 回傳 `[]`；`zip_segments` 回傳 `()` | ER-32 |
| `services/url_policy.py`（L3） | `validate` 丟 `ValueError('stub')`；`is_safe` 回傳 False | IC-R01 |
| `controllers/streaming.py`（L4） | `uniform_404`、`service_busy`、`json_response`、`limit_page` 回傳最小的 Response（固定 bytes、status 對應）；`serve`、`create_batch`、`zip_response` 丟 NotImplementedError；`MAX_UPLOAD_BODY`、`max_upload_body` 用最終值 | ER-32 |
| `services/zip_builder.py`（L4） | dataclass 和例外用最終定義；`build_zip` 丟 NotImplementedError | IC-R01 |
| `models/trash_actions.py`、`cron_jobs.py`、`zip_actions.py`、`backend_actions.py`、各精靈 | 公開方法的第一個陳述句是權限檢查，接著丟 NotImplementedError；`_cron_*` 的第一個陳述句是 su 檢查 | IC-R01 |
| controllers（L4、L7） | 只有類別，沒有任何 route。`tools/routes.lock.json` 第 0 天是空清單，各 lane 新增 route 時自己更新 | IC-R12 |

---

## 4. IF-STATIC：static tools、標記、白名單

- **提供者**：L0。
- **使用者**：L1～L7、L11、L12（L8～L10 間接受它管轄）。

### 4.1 指令列介面
- 每支工具都是 `python3 -I tools/check_<name>.py [--g1] [--root <repo>]`。
  - 輸出每行 `<path>:<line> <RULE> <message>`。
  - 結束碼：0＝通過，1＝有違規，2＝工具本身不能用（CI 視為失敗）。
- `tools/run_all.sh [--g1]` 依序跑 8 支，任何一支非 0 就非 0。
- `--g1` 模式：sudo 計數改成「剛好等於」；`WOOW-STUB` 必須是 0；entry points 必須剛好 10 筆，而且等於 target。
- `check_routes.py --emit-entry-points`：輸出 JSON 陣列。每個 decorated route 函式一筆（main 4、portal 5），再加上固定的一筆 `CustomerPortal._prepare_home_portal_values`，合計 10 筆（ER-47.5、T-STAT-17）。

### 4.2 原始碼標記（同一行的註解）

| 標記 | 意義 |
|---|---|
| `# sudo-ok: S1`／`S3` | sudo 白名單 |
| `# sql-ok: Q2`～`Q8`、`Q17`～`Q21` | raw SQL 白名單（現有的代號，以 `tools/sql_allowlist.yaml` 為準；Q1 已移除） |
| `# WOOW-STUB` | L0 寫的 stub，G1 時必須是 0 筆 |
| CSV 那一列旁邊的 `HD-07` | portal 對 folder 的 W=1 只是探測用（check_security_matrix 檢查） |

### 4.3 sudo 白名單（check_sudo；ER-25、ER-44）

| 代號 | 檔案 | model（判斷方式） | 函式 | 數量 |
|---|---|---|---|---|
| S1 | `W/models/document_log.py` | `_name='woow.document.log'` | `_log_event` | 1：`self.sudo().create(vals)` |
| S3 | `W/models/settings.py` | `_name='woow.document.settings'` | `_read_params` | 1：`self.env['ir.config_parameter'].sudo().get_param(k)` |

- 非測試程式裡，`with_user(` 是 0 次。
- 整個 repo 不准出現 `<名稱以 _sudo 結尾>.env`（T-STAT-19）。
- 每個 `_cron_*` 的第一個陳述句都必須檢查 `self.env.su`。

### 4.4 raw SQL 白名單（check_sql；`tools/sql_allowlist.yaml`）

| 代號 | 檔案::函式 | 內容 |
|---|---|---|
| Q2 | `models/quota.py::_usage_bytes` | arch §6.4 Q2；以自己的連線讀已 commit 的用量（見 §7.2） |
| Q3 | `models/document_folder.py::init`（名稱唯一索引）、`models/document_tag.py::init`（啟用中標籤的名稱與公司唯一索引） | 只准固定字串的 DDL（ER-47.4） |
| Q4 | `services/locks.py`（`try_lock`、`lock`、`bounded_wait`） | `SELECT pg_try_advisory_xact_lock(%s)`、`SELECT pg_advisory_xact_lock(%s)`、`SELECT current_setting('lock_timeout')`、`SELECT set_config('lock_timeout', %s, true)` |
| Q5 | `models/quota.py::_bytes_if_new` | arch §6.4 Q5 |
| Q6 | `models/ir_attachment.py`（守衛） | arch §6.4 Q6 |
| Q7 | `models/folder_access.py::_check_member` | 在存取交易鎖之下，不經 record rule 數這個資料夾的全部授權列（授權上限的判斷） |
| Q8 | `models/folder_access.py::_lock_folders` | 新增授權之前，在 Repeatable Read 下強制列版本衝突，讓並行的授權變更序列化；非 su 的呼叫者要先過 model 權限、id 的形式與 manage 權限（`_woow_manageable_folders`），之後才取鎖 |
| Q17 | `models/ir_attachment.py::_woow_field_attachment_ids`、`::_woow_document_file_row` | 要加進討論的附件中，哪些是某個 binary 欄位的附件；文件記錄的檔案附件（`attachment_id`，且是不帶 URL、屬於該文件 `datas` 的 binary 附件）的儲存位置，串流只從這一列建立 |
| Q18 | `models/mail_thread_guard.py::_woow_follower_scope`、`::_woow_user_companies` | 通知對象所在記錄的讀取規則所依據的儲存欄位（state、公司、eff_* 授權、直接分享），以及被通知帳號的公司 |
| Q19 | `models/document_folder.py::_woow_check_name_free` | 同一層、同公司、啟用中的資料夾是否已用這個名稱（與唯一索引相同的判斷），先回 M-02 |
| Q20 | `models/document.py::_woow_portal_direct_documents` | 直接分享給呼叫者本人聯絡人的文件 id，再以呼叫者的 record rule 搜尋 |
| Q21 | `models/trash_actions.py::_woow_lock_purge_root` | purge 讀任何 hold 之前，以 `SELECT … FOR NO KEY UPDATE` 依 id 順序鎖住這個根資料夾與子樹裡每個資料夾的列（子樹以 active_test=False 的 child_of 取得，含個別丟進回收桶的資料夾；只鎖不寫列版本）：本批快照之後 commit 的 legal hold、還原或搬移改過其中任一列，就 serialization_failure、跳過這個根；之後才開始的會等這一批結束 |

- 表以 `tools/sql_allowlist.yaml` 為準（`check_sql --g1` 要求每一筆都被用到），這裡列出它的每個代號；Q1、Q9～Q16、Q22、Q23 已移除（Q10 是配額的列版本屏障，其餘隨連結式分享移除），代號不再使用。
- 其他地方出現 `pg_*advisory*`，或 locks.py 裡呼叫內建的 `hash(`，都直接失敗（T-STAT-22）。
- tag、access、`attachment_id` 的唯一索引，一律用 ORM 或 `odoo.tools.sql.create_unique_index(cr, name, table, [固定運算式])` 建在各自 model 的 `init()` 裡。這不是 raw SQL，不算 Q3（IC-R07）。

### 4.5 check_routes（sec §7，再套用 ER-22、ER-27、ER-47.2）
- R1～R13 照 sec §7，另有以下修改：
  - **R3**：`auth` 一律寫成 `'user'`；`auth='public'` 與 `auth='none'` 都不允許。
  - **R6**：沒有任何 `<path:>` 例外。
  - **R8**：上傳路由要寫 `max_content_length=95 * 1024 * 1024`（整數字面量的乘法運算式，檢查器把它算成 99,614,720 比對）。如果寫的是 callable，名稱只能是 `max_upload_body`（IC-R03）。
  - **R11**：已退役（沒有 public 的路由，R3 已涵蓋）；其他規則的編號不變。
- `tools/routes.lock.json` 每一列是 `{file, func, routes[], methods[], auth, type, website}`，屬於 always_human 範圍。

### 4.6 check_forbidden（arch §6.2 加 sec §8，再加 ER 的新規則）
- ER-29：在 controllers 裡用 raise、abort 或 return 回傳 not_found（T-STAT-23）。
- ER-35：`force_send=True`（T-STAT-25）。
- ER-04：rules.xml、controllers 和 models 裡出現 `commercial_partner_id`（T-STAT-24）。
- R2b-02、R2b-03：`.touch(`；對 `request.session[...]` 做 subscript 指派、`.pop(`、`.update(` 這類原地修改（T-STAT-21）。
- F-TOKEN-API（arch §6.2）：非測試程式不准呼叫 `limited_field_access_token`、`generate_access_token`。唯一的例外是 `models/ir_attachment.py` 裡名為 `generate_access_token` 的覆寫，以 `super()` 把其他 model 的附件交給 core（§8.6）；別的檔案、別的函式、不經 `super()` 的呼叫，或在覆寫裡呼叫的是別的 token 函式（例如 `super().limited_field_access_token(...)`）都照舊違規（`tools/tests` 的 fixtures 釘住這四種）。

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
woow.document::action_open_move_wizard
woow.document.folder::action_trash
woow.document.folder::action_restore
woow.document.folder::action_purge
woow.document.folder::action_set_legal_hold
woow.document.folder::action_download_zip
woow.document.folder::action_open_move_wizard
woow.document.folder::action_notify_members
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
woow.document::message_unsubscribe  woow.document::message_new
woow.document.folder::message_post  woow.document.folder::message_subscribe
woow.document.folder::message_unsubscribe  woow.document.folder::message_new
res.config.settings::set_values  res.config.settings::default_get
woow.document.*.wizard::default_get
ir.attachment::generate_access_token  ir.attachment::validate_access
```
清單以 `tools/public_methods.txt` 為準，`check_public_methods` 讀的是那個檔案。

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

EVENTS: tuple[str, ...] = (  # 12 種，順序固定（ER-37）
    'view', 'download', 'zip', 'upload', 'replace', 'trash', 'restore', 'purge', 'move',
    'access_change', 'legal_hold', 'settings_change',
)
CHANNELS: tuple[str, ...] = ('backend', 'portal', 'rpc', 'cron')
RESULTS: tuple[str, ...] = ('ok',)

WOOW_MODELS: tuple[str, ...] = (
    'woow.document', 'woow.document.folder', 'woow.document.folder.access',
    'woow.document.tag', 'woow.document.log',
)
TRASHABLE_MODELS: tuple[str, ...] = ('woow.document', 'woow.document.folder')   # I-04

# context 鍵
CTX_LOG_CHANNEL = 'woow_log_channel'       # 值只能是 'backend' 或 'rpc'；其他值一律忽略
CTX_LEGAL_HOLD = 'woow_legal_hold'         # action_set_legal_hold(hold=None) 的預設值來源
CTX_NOTICE_URL = 'woow_notice_url'         # 成員通知信範本
CTX_NOTICE_NOTE = 'woow_notice_note'

# 上限和數值
MIB = 1024 * 1024
UPLOAD_HARD_CAP_MB = 95
MAX_UPLOAD_BODY = 95 * 1024 * 1024         # 99_614_720（ER-03、ER-47.2）
MAX_ACCESS_ROWS_PER_FOLDER = 500
TREE_QUERY_LIMIT = 1000
PORTAL_PAGE_SIZE = 20
ZIP_DEADLINE_S = 90
ZIP_SPOOL_MAX_BYTES = 32 * MIB
PIXEL_LIMIT = 50_000_000
FILENAME_MAX_CHARS = 200
SEGMENT_MAX_BYTES = 255
URL_MAX_LEN = 2048
USER_AGENT_MAX = 256
LOCK_TIMEOUT_MS = 5000
MAIL_PER_USER_PER_HOUR = 100               # 一次資料夾通知最多的收件人數
PURGE_BATCH = 200
LOG_RETENTION_BATCH = 5000
POISON_ALERT_DAYS = 3
```

注意：`services/locks.py` 也有一個 `RANK`，那是鎖的取得順序（ER-24），和這裡的權限等級是兩回事。引用時一律帶模組名稱，例如 `constants.RANK`、`locks.RANK`。

---

## 6. IF-SET 和 IF-IP

### 6.1 IF-SET：`W/models/settings.py` 和 `W/models/res_config_settings.py`
- **提供者**：L1。
- **使用者**：L2、L3、L4、L6、L7、L8、L12（L11 唯讀）。

```python
PARAM_PREFIX = 'woow_document_management.'
_PARAM_KEYS: tuple[str, ...]          # 下表 21 個鍵（不含前綴），順序同下表
_READABLE_KEYS: frozenset[str] = frozenset(PARAM_PREFIX + k for k in _PARAM_KEYS) | {'database.secret'}

@dataclasses.dataclass(frozen=True)
class WoowDocSettings:   # 欄位名稱＝鍵名稱；型別見下表
    ...

class WoowDocumentSettings(models.AbstractModel):
    _name = 'woow.document.settings'
    _description = 'Document management settings reader'

    def _read_params(self, keys: Iterable[str]) -> dict[str, str | None]:
        """S3。鍵不在 _READABLE_KEYS → ValueError。只回傳原始字串。"""
    def _get_settings(self) -> WoowDocSettings:
        """型別轉換和範圍檢查。任何鍵失敗，就用該鍵的預設值（無效值一律退回預設）。不寫任何參數（I-12）。"""
    def _audit_ip_key(self) -> bytes:
        """HMAC-SHA256(database.secret, b"woow_document_management/audit-ip/v1")，只在記憶體衍生金鑰。database.secret 是空的或讀不到 → ValueError。"""
```

| 鍵 | 型別（dataclass） | 預設 | 範圍 |
|---|---|---|---|
| trash_retention_days | int | 30 | 0～3650（0＝不自動清除；core 把設定表單的整數 0 存成沒有值，所以 `set_values` 以字串 `'0'` 寫回） |
| notice_base_url | str | '' | 必須是 https URL（只有協定、主機和埠；通知信用，空值時用站台網址） |
| allow_http_links | bool | False | — |
| portal_max_upload_mb | int | 25 | 1～95 |
| internal_max_upload_mb | int | **95** | 1～95（ER-03） |
| max_files_per_upload | int | 10 | 1～20 |
| extra_extensions | tuple[str, ...] | () | 只能是 `mime_map.OPTIONAL_EXTENSIONS` 的子集 |
| zip_max_files | int | 2000 | 1～5000 |
| zip_max_mb | int | 500 | 1～2048 |
| zip_max_depth | int | 20 | 1～50 |
| zip_concurrency | int | 2 | 1～4 |
| quota_mb | int | 0 | 0～10^7（0＝不限） |
| quota_warn_pct | int | 80 | 1～99 |
| quota_block_pct | int | 95 | warn＋1～100 |
| log_retention_days | int | 365 | 30～3650 |
| log_ip_mode | str | truncate | truncate、hash、none |
| allow_root_create | bool | False（ER-05） | — |
| tag_create_policy | str | managers | managers、users |
| trusted_proxies | tuple[ip_network, ...] | () | CIDR 清單（經 `client_ip.parse_trusted_proxies` 解析） |
| client_ip_header | str | '' | ''、CF-Connecting-IP、X-Real-IP、X-Forwarded-For |
| uninstall_confirm | str | '' | 原樣保留 |

任何一個鍵不是合法值（型別或範圍不符、不在選項裡）時，`_get_settings` 一律用該鍵的預設值；`set_values` 以嚴格模式檢查，不合法就丟 ValidationError。

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
- **使用者**：L3（`_log_event`）、L12（G-IP）。

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
- `ip_hash` 永遠是 `hmac_ip(settings._audit_ip_key(), 完整 ip)`（§6.1；金鑰的 label 沒變，已存的雜湊仍可比對）。
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
- PNG、JPEG、GIF 的標頭結構在 Pillow 開啟之前先計數（Pillow 以 Python 逐一讀取，有些結構的成本比大小高：GIF 逐段串接註解、JPEG 保留每個應用程式區段、PNG 保留每個私有 chunk）：PNG 計到第一個 IDAT 為止的 chunk、JPEG 計到影像結束（EOI）為止的區段與 scan（SOS）、GIF 計到第一個圖像之前的擴充區塊與其子區塊，超過 `IMAGE_MAX_STRUCTURES`（4096），或結構之外的雜訊位元組超過 `IMAGE_MAX_JUNK`（4096）→ `'content'`，不呼叫 `Image.open`。JPEG 的 scan 另有上限 `JPEG_MAX_SCANS`（64；編碼器寫 1、6、10 或 18 個）：解碼器對每個 scan 走過整張圖，Odoo 儲存與縮圖時都會解碼；scan 的資料以一次搜尋跳到下一個標記，EOI 之後的位元組不看。
- PNG 另有更緊的限制，因為有些 chunk 的成本遠高於大小：Pillow 會解壓色彩設定檔（`iCCP`）和壓縮文字（`zTXt`，以及壓縮的 `iTXt`），每個最多 1 MiB，而幾百位元組就能解成 1 MiB（約 1000 倍），又只限制文字 chunk 的總量（64 MiB，`iCCP` 沒有累計上限）；4000 個約 1 KB 的 `iCCP`（約 4 MB，在每個大小上限之內）會讓 `Image.open` 跑十幾秒。`Image.open` 只讀到第一個 IDAT 為止的 chunk，但 `load()` 讀到 IEND，而 Odoo 儲存附件時會載入 PNG（`ir.attachment` 的後處理讀它的方向或縮圖），所以 IDAT 之後的 chunk 一樣有成本。因此 `_png_structure_ok` 走過整個檔案到 IEND：不是影像資料的 chunk 最多 `PNG_MAX_CHUNKS`（256；一般檔案不到 20 個）個、色彩設定檔最多 `PNG_MAX_PROFILES`（1，PNG 規格只允許一個）個、`zTXt` 加 `iTXt` 最多 `PNG_MAX_COMPRESSED_TEXTS`（16）個；影像資料的 chunk（`IDAT`，動畫的 `fdAT` 與 `fcTL`）沒有固定的個數上限，但函式庫以 Python 逐個讀它們，空的或很小的 chunk 成本遠高於大小，所以個數上限是 `PNG_FREE_IMAGE_CHUNKS`（4096）加上檔案大小每 `PNG_IMAGE_CHUNK_BYTES`（256）位元組一個（額度隨檔案大小成長：一張大圖重切成幾千個小 chunk 仍被接受，同樣個數的空 chunk 放在小檔案裡則被拒絕；這也讓檢查本身走過的 chunk 數有上限）。任何一項超過 → `'content'`，不呼叫 `Image.open`（JPEG 與 GIF 仍用 `IMAGE_MAX_STRUCTURES`）。`tEXt` 的成本等於它的大小（4000 個約 10 毫秒），只受 chunk 總數限制。

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
        量測只在這次執行取得的配額鎖仍被持有時才保留：鎖持有到交易結束，但取得鎖的那次呼叫若被呼叫端回滾到
        savepoint，在 savepoint 裡取得的鎖會一併釋放（加入資料夾精靈對每個來源各開一個 savepoint）。所以執行中
        還沒有接受任何位元組就被拒絕的呼叫（可能就是取得鎖的那一個）會丟掉量測，下一次呼叫取得鎖之後重新量測，
        不會用已沒有鎖保護的舊量測放行；已接受位元組之後鎖仍在，量測不變。呼叫端可以回滾被拒絕的呼叫，
        但回滾了已接受的呼叫之後不可再使用同一個 ledger。
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
- **使用者**：L3、L4、L7。

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
- **使用者**：L4（ZIP）、L2（quota）、L6（purge 的 `bounded_wait`，IC-R09）。

```python
class LockBusy(Exception): ...
RANK: dict[str, int] = {'zip': 1, 'quota': 2, 'access': 3}
NAME_RE = re.compile(r'^(zip:u:\d+|zip:slot:[1-4]|quota(?::warn|:u:\d+)?|access:\d+)$')

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
| L4 ZIP | `lock(cr, 'zip:u:<uid>')`（會等；逾時就是 `LockBusy`），再對 i＝1..zip_concurrency 逐一 `try_lock('zip:slot:<i>')`（只有 slot 用 `try_lock`） | `zip:u` 逾時：`LockBusy` → `zip_response` 回傳 `service_busy()`（503）；沒有空的 slot：回傳 `limit_page('M-43', 429)` |
| L2 配額 | `lock(cr, 'quota:u:<uid>')`，再 `lock(cr, 'quota')`（同為 rank 2） | `LockBusy` → 由路由回傳 `service_busy()` |

---

## 8. 核心 model、權限、log、守衛（L3）

### 8.1 IF-MODEL
- **提供者**：L3。
- **使用者**：L4、L6、L7、L8、L10、L11。

**guard mixin（`models/guard_mixin.py`）**
```python
class WoowServerFieldsMixin(models.AbstractModel):
    _name = 'woow.document.server.fields.mixin'
    _woow_own_default_fields: frozenset[str] = frozenset()   # 各 model 宣告：伺服器設定、或 create 帶入時會被檢查或拒絕的欄位
    @api.model
    def _add_missing_default_values(self, values):
        """values 沒有明確帶入的 _woow_own_default_fields 欄位只取欄位自己的 default（沒有就不填），不論呼叫者是不是 su。
        core 的 default_get 還會讀呼叫者的 ir.default（每位內部使用者都能存自己的，管理員能存全站的；
        sudo() 保留呼叫者的 uid，也就保留他存的預設值）與 context 的 default_*；欄位 groups 對它們不適用，
        而 create 的檢查只看明確帶入的鍵。只有 create 使用：default_get、表單預填與明確帶入的值都不變。"""
def own_default_values(model, values, completed, names):   # 上面的做法本身；ir.attachment 也用它

class WoowDocumentGuardMixin(models.AbstractModel):
    _name = 'woow.document.guard.mixin'
    _inherit = ['woow.document.server.fields.mixin']
    _woow_allowed_defaults: frozenset[str] = frozenset()   # 各 model 覆寫，例如 {'default_folder_id'}
    @api.model_create_multi
    def create(self, vals_list):
        """清掉 context 裡不在白名單的 default_*。
        非 su 時，補做欄位 groups 檢查（CA-10），沒權限 → AccessError。su 時略過（ER-45.4）。"""
    def copy(self, default=None):          # folder、document 一律丟 UserError
    def _require_manager(self) -> None:     # 不是 su，也不在 group_woow_document_manager → AccessError
    def _woow_log_channel(self) -> str:     # su → 'cron'；share 使用者 → 'portal'；context 是 backend 或 rpc 就用它；其他 → 'rpc'
```

**伺服器設定的欄位（`_woow_own_default_fields`）**：每個 model（含 transient）都繼承 `woow.document.server.fields.mixin`（直接或經 guard mixin）並宣告自己的清單，沒有這類欄位時是 `frozenset()`；只有欄位全由使用者在表單填寫的精靈（move、restore、purge、add.attachment、notify）例外。`tools/tests/test_static_checks.py` 的 `TestServerFieldsDeclared` 在新 model 沒有宣告時失敗。清單：
- `woow.document`：`document_type`、`datas`、`url`、`mimetype`、`owner_id`、`uploaded_by_partner_id`、`shared_user_ids`、`shared_partner_ids`、`source_res_model`、`source_res_id`、`legal_hold`、`active`、`trashed_date`、`trashed_by_id`、`restored_date`，由檔案、資料夾與日期算出的 `attachment_id`、`checksum`、`file_size`、`company_id`、`folder_in_trash`、`woow_user_can_write`、`source_display`，以及 chatter 的 `activity_ids`、`message_follower_ids`、`message_ids`、`website_message_ids`；
- `woow.document.folder`：`_SERVER_ONLY_FIELDS`、10 個 `eff_*`、`company_id`、`legal_hold`、`inherit_access`、`active`、`trashed_date`、`trashed_by_id`、`restored_date` 與同樣的 chatter 欄位；`owner_id` 不在清單：沒有 manage 的人 create 的 `owner_id` 一律被上層資料夾的擁有者蓋過；有 manage 的人與建立根資料夾的人可以明確帶入，沒帶入時由呼叫者存的 `ir.default` 補上的也一樣，但帶入的與補上的都要過 `_woow_check_owner`（owner 要是文件使用者；不繼承的資料夾，非文件管理員另要 owner 在上層有 manage 權，見「資料夾的擁有者與授權上限」）；
- `woow.document.folder.access`：`company_id`；`woow.document.tag`：空（每個欄位都是建立者的選擇）；
- `woow.document.log`：全部欄位（`test_reg_server_columns_ignore_defaults.py` 檢查清單等於全部欄位）：只有伺服器寫入，`_log_event` 的稽核列自己帶齊每一欄，直接 create 沒帶的欄位只取欄位自己的 default；
- `ir.attachment`（`ir_attachment.py` 的 `_add_missing_default_values`，用同一個 helper）：`res_model` 在 `WOOW_MODELS` 且有 `res_field` 的附件，也就是文件與資料夾 binary 欄位的檔案，其 `public`、`access_token`、`url`、`description`、`original_id`、`company_id`：欄位機制以 su、呼叫者的 uid 建立這個附件，它不會是 public 或帶 token。

**`woow.document.folder`（`models/document_folder.py`）**
- 欄位照 arch §3.1，**另外有以下這些**（其他 lane 會用到的名稱一律照這裡寫）：
  - 10 個 eff 欄位：`eff_read_user_ids`、`eff_upload_user_ids`、`eff_edit_user_ids`、`eff_manage_user_ids`、`eff_read_group_ids`、`eff_upload_group_ids`、`eff_edit_group_ids`、`eff_manage_group_ids`、`eff_read_partner_ids`、`eff_upload_partner_ids`。rel 表是 `woow_doc_folder_eff_{r,u,e,m}_{user,group}_rel` 和 `woow_doc_folder_eff_{r,u}_partner_rel`。
  - `in_trash`、`has_restricted_descendant`、`subtree_legal_hold`：stored、recursive。`has_restricted_descendant`、`subtree_legal_hold` 以 `with_context(active_test=False).child_ids` 計算：個別丟進回收桶的子資料夾也算，儲存值不受重算時 context 影響；`init()` 在每次安裝與更新時呼叫 `_woow_recompute_subtree_flags()` 重算所有資料夾的這兩個欄位。
  - `woow_user_rank`：Integer，非 stored，`@api.depends_context('uid')`，值是 0～4。文件管理員和 su 是 4。
  - `woow_has_portal_members`：Boolean，非 stored，條件是 `bool(eff_read_partner_ids)`（ER-15）。
  - `effective_access_lines`：Json。`document_count`、`subfolder_count`：在使用者 env 用 `_read_group` 計算。
  - 舊模組的 `policy_flag` **不存在**（ER-36）。

```python
    @api.model
    def _woow_rank_domain(self, n: int, user=None) -> list:
        """E(n)，內容見 arch §4.4；對象是 user，沒給就是呼叫者（`_woow_check_owner_ceiling` 用它求 owner 的 manage 權）。"""
    def _woow_filter_rank(self, n: int) -> 'woow.document.folder':
        """self 裡通過 _woow_has_rank(n) 的資料夾，只用一次 search；唯一的呼叫端是 _woow_has_rank，一次傳一個資料夾。"""
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
        不寄信，不自動加 follower（ER-45.4）。
        _woow_own_default_fields（見上）：上面這些檢查只看明確帶入的鍵，沒帶入時一律是欄位自己的 default。
        非 su 的文件使用者：新資料夾的 owner（明確帶入的；沒帶入就是 `default_get` 的值）過 `_woow_check_owner`，
        `inherit_access` 依 ORM 存下的值判斷，不看帶入的值是什麼型別（`_stored_flag`：有帶這個鍵就是 `bool(值)`，沒帶就是欄位的預設 True；
        `0`、`0.0`、`None`、空字串與空 list 和 `False` 一樣是不繼承，`1`、非空字串與 `'false'` 是繼承），編輯者檢查與 owner 檢查都用它；
        編輯者（對上層只有 edit）不能選 owner、不能關閉繼承，owner 一律是上層的 owner，不檢查；
        不是文件使用者的呼叫者交給 model 的 ACL，不看 owner。"""
    def write(self, vals):
        """非 su：company_id 只有文件管理員能改（改成別家公司或改成空值；寫入目前的值不算變更），
        只有 manage 的成員不行：根資料夾改成空值或別家公司之後，子資料夾可以逐層改，等於把整棵子樹交給別家公司的管理員。
        parent_id 清空（搬到最上層）而 self 裡有資料夾原本有上層 → 和 create 一樣的根資料夾政策（M-24），
        先於名稱檢查，所以同名與否的答案相同；原本就在最上層的不擋。
        `inherit_access` 依 ORM 存下的值判斷，不看帶入的值是什麼型別（`_stored_flag`）：值裡有這個鍵而且 `bool(值)` 為假就是關閉繼承
        （`False`、`0`、`0.0`、`None`、空字串、空 list），同一次寫入不能再帶 `access_ids`（`Save member changes before disabling inherited access.`）
        或 `parent_id`（`Save the new parent before disabling inherited access.`）；仍在繼承的資料夾先過成員列的授權上限（`_check_parent_ceiling`），
        再把繼承來的權限轉成成員列，誰都不失去權限（文件管理員與 su 的寫入也一樣）；這三處與 `_woow_check_owners` 讀的是同一個值，
        `True`、`1`、非空字串（含 `'false'`）照真。
        非 su：值裡有 owner_id、parent_id 或 inherit_access 時，在名稱檢查之後、任何寫入之前呼叫 `_woow_check_owners`；
        owner 改變（含清空）寫一列 `access_change`（§8.4）。"""
    @api.model
    def _woow_owner_value(self, value) -> 'res.users':
        """owner_id 的值對應的 res.users（空＝沒有 owner）；不是 id 的值（字串、浮點數、布林、0、負數）→ ValidationError('Choose a document user.')。"""
    def _woow_check_owners(self, vals) -> None:
        """依資料夾寫入後的上層與 inherit_access：owner 改成別人 → `_woow_check_owner`（寫入現有的 owner 不算變更）；
        owner 不變而資料夾搬到別的上層或停止繼承 → 只做 `_woow_check_owner_ceiling`；相同 owner、上層、繼承狀態的資料夾只問一次。"""
    @api.model
    def _woow_check_owner(self, owner, parent, inherit_access) -> None:
        """su 不檢查；其他人：owner 要是內部使用者而且屬於 group_woow_document_user，否則 ValidationError('Choose a document user.')，
        再過 `_woow_check_owner_ceiling`。"""
    @api.model
    def _woow_check_owner_ceiling(self, owner, parent, inherit_access) -> None:
        """資料夾不繼承（inherit_access 為假）而且有上層時，owner 要在上層有 manage 權，否則
        ValidationError('A member cannot be granted more access than on the parent folder.')（與 `folder_access._check_parent_ceiling` 同訊息）。
        不檢查：su、文件管理員的呼叫者、文件管理員的 owner（不需要授權）、繼承中的資料夾、沒有上層、沒有 owner。
        上層的授權以呼叫者的身分搜尋（`search_count`，套用呼叫者的 record rule），沒有 sudo、沒有 raw SQL。"""
    def init(self):
        """Q3（固定字串）：woow_document_folder_name_uniq ON
        (COALESCE(parent_id,0), COALESCE(company_id,0), lower(name)) WHERE active。  # sql-ok: Q3"""
```

**資料夾的擁有者與授權上限（AC-2；2026-11-13）**
- owner 在 `_compute_effective` 一律得到 manage（rank 4），所以「不繼承的資料夾只能收窄上層的授權」（AC-2）涵蓋 owner，和涵蓋成員列一樣：不是文件管理員的人，不能讓在上層沒有 manage 權的人成為這種資料夾的 owner。原本只有成員列受限（`folder_access._check_member` 呼叫 `_check_parent_ceiling`）。
- owner 要是文件使用者（arch §3.1）：除了 su，指定 owner 的 create 與 write 都檢查（內部使用者，而且屬於 `group_woow_document_user`），否則 `ValidationError('Choose a document user.')`，與成員列同一個訊息，翻譯目錄不增加。owner 不存在、值不是 id（字串、浮點數、布林、0、負數）也是同一個答案；清空 owner（`False`、`None`）不檢查。沒有全域的 `@api.constrains('owner_id')`：su 建立資料夾時 owner 預設是 su 自己，常不是文件使用者。
- 誰受哪些限制：su 都不受；文件管理員只受「要是文件使用者」；其他人兩項都受，授權上限只在資料夾（寫入或建立之後）不繼承而且有上層時生效（繼承中的資料夾連成員列都沒有上限）。新 owner 要在上層有 manage 權，也就是 owner 的 `_woow_has_rank(4)`：列在上層的 `eff_manage_user_ids`、經 `eff_manage_group_ids` 的群組，或是文件管理員（不需要授權），否則 `ValidationError('A member cannot be granted more access than on the parent folder.')`，與成員列同一個訊息。
- 檢查的位置：`create`（見上）；`write` 的 `_woow_check_owners`，依資料夾寫入後的上層與 `inherit_access`：owner 改成別人 → 完整檢查；owner 不變而資料夾搬到別的上層，或從繼承改成不繼承 → 現有的 owner 要過授權上限。所以授權上限不能分兩步繞過：先在資料夾繼承時指定 owner 再關閉繼承，或搬到 owner 沒有權限的上層。`inherit_access` 與 `owner_id` 的值依 ORM 存下的內容判斷（`0`、`None`、空字串、空 list 都是關閉繼承；`inherit_access` 只有一種讀法，`_stored_flag`，create 的編輯者檢查、write 的兩個「先存」檢查與轉成成員列也用它，見上）。create 的 owner 包含沒帶入時由呼叫者存的 `ir.default` 補上的值；`write` 同一個 owner、上層與繼承狀態的資料夾只問一次（每次是一次 `search_count`，第一次問某個 owner 時多讀一次他的群組；文件使用者與文件管理員的身分來自 ORM 的快取），沒有帶這三個鍵的 write 不呼叫檢查；繼承中的資料夾與文件管理員的呼叫不做授權上限的搜尋；owner 改變時每個資料夾多一個 INSERT（稽核列）。
- 看不到上層的呼叫者：上層的授權以呼叫者的身分搜尋（`search_count`，套用呼叫者的 record rule），沒有 sudo、沒有 raw SQL，sudo 與 SQL 白名單不變（S1、S3；Q 代號不增加）。呼叫者讀不到上層時搜尋沒有結果，答案和「owner 在上層沒有權限」相同（同一個 `ValidationError`），不論 owner 是誰：所以這個檢查不能拿來探測看不到的上層有哪些成員。代價：讀不到上層的 manager 不能把這種資料夾交給別人（含在上層有 manage 權的人），要由文件管理員處理；成員列在同一情況丟 AccessError，沒有改。
- 稽核：owner 改變（含清空）每個資料夾寫一列 `access_change`（§8.4）；建立資料夾、寫入同一個 owner、被拒絕的寫入都不記。
- 行為改變：不是文件管理員的 manager，不能再把不繼承的資料夾交給在上層沒有 manage 權的人，也不能在 owner 沒有權限的上層底下搬動它或讓它停止繼承；要先在上層給 manage 權，或由文件管理員處理。指定不是文件使用者的 owner，除了 su 都被拒絕。
- 測試：`test_reg_owner_within_ceiling.py`。

**`woow.document.folder.access`（`models/folder_access.py`）**
- 欄位：`folder_id`、`company_id`、`subject_type`、`user_id`、`group_id`、`partner_id`、`level`。
- 限制照 arch §3.2。唯一索引用 `create_unique_index` 建（IC-R07）。
- 改到 access 會記 `access_change`；資料夾的 owner 改變也記一列（見上，§8.4）。建立時不寄信、不加 follower（ER-45.4）。
- `company_id` 是 `folder_id.company_id` 的 stored related 欄位，ORM 不拒絕對它的寫入；`write` 對非 su 的呼叫者檢查：`company_id` 與該列資料夾（同一次寫入也帶 `folder_id` 時為目標資料夾）的公司不同 → AccessError（沿用 `This folder field cannot be changed directly.`；寫入目前的值不算變更，`_company_value` 解析 id、recordset、tuple）。`create` 帶的 `company_id` 會被 `modified(create=True)` 重算成資料夾的公司。

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
        - 非內部使用者只接受 name、folder_id、datas、description，伺服器設定 uploaded_by_partner_id，強制 tracking_disable；description 最多 `PORTAL_DESCRIPTION_MAX_CHARS`（2000）字元，超過 → `WoowUploadRejected('content')`，在配額鎖與寫入之前拒絕（core 對這個 HTML 欄位 sanitize 兩次）；
        - 內部使用者帶 mail_create_nolog 和 mail_create_nosubscribe；
        - 每份文件用 S1 記一筆 upload；
        - _woow_own_default_fields（見上）：上面的檢查只看明確帶入的鍵，沒帶入時一律是欄位自己的 default；
        - 帶 NO_INDEX_CTX_KEY 建立與寫入，文件的檔案附件不產生 index_content（§8.6）。"""
    def write(self, vals):   # datas → _validate、記 replace；folder_id → 目標 ≥ upload、不在回收桶、同公司，記 move；shared_* 需要 manage
    def unlink(self):        # 只有文件管理員或 su；有 legal_hold → UserError(M-34)
```

**`woow.document.tag`（`models/document_tag.py`）**
- create 依 `tag_create_policy` 檢查，su 時略過。

**ER-45.4 的承諾（同時也是 IF-RB-2、IF-RB-3）**
- guard mixin 的欄位 groups 檢查在 `env.su` 時略過。
- 建立 folder 和 access 時，不寄任何信、不自動加 follower。成員通知只能透過 L8 的精靈。
- 這兩項如果有任何改動，L3 都要通知 L10，L10 再重跑 T-RB-02～T-RB-05。

### 8.3 IF-FOLD：`W/services/folding.py`（純 Python）
- **提供者**：L3。
- **使用者**：L4、L7（L3 自己的 display_name 也用它）。

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
- **使用者**：L1、L4、L6、L7、L8、L11。

```python
class WoowDocumentLog(models.Model):
    _name = 'woow.document.log'
    @api.model
    def _log_event(self, event: str, *, channel: str,
                   user_id: int | None = None, partner_id: int | None = None,
                   document_id: int | None = None, folder_id: int | None = None,
                   company_id: int | bool | None = None,
                   ip: str | None = None, user_agent: str | None = None,
                   detail: Mapping[str, int | bool | list[int]] | None = None) -> int:
        """S1。
        - event、channel 不在 constants 的清單裡 → ValueError（屬於程式錯誤）；
        - detail 的值只能是整數、布林值或整數清單，否則 ValueError；
        - ip 沒有傳入，而且有 http request 時，用 client_ip.request_client_ip 取得；user_agent 先移除控制字元（含 NUL），再截斷到 256；
        - user_id 預設是 env.uid（public 使用者記 None）；company_id 預設（None）是 env.company.id，False（沒有公司的資料夾，
          `folder.company_id.id` 讀出來就是 False）記成空公司，R-L0 讓每家公司的管理員都讀得到；其他型別 → ValueError。
        self.sudo().create(vals)   # sudo-ok: S1   → 回傳 1。vals 帶齊每一欄（`result` 一律是 'ok'），所以呼叫者存的預設值不會進到稽核列。"""
```

**事件 → 記錄的 lane**

| 事件 | lane |
|---|---|
| upload、replace、move、access_change | L3 |
| view、download、zip | L4 的 `serve` 和 `zip_response`（portal 也經由它們） |
| trash、restore、purge、legal_hold | L6 |
| settings_change | L1 |

- legal_hold 的 detail 是 `{"on": 0或1, "doc": id}` 或 `{"on": 0或1, "folder": id}`（ER-37）。
- access_change 的 detail：成員列是 `{"member_id": id}` 或 `{"member_ids": [id, …]}`；資料夾的 owner 改變是 `{"owner_id": 新的 id, "previous_owner_id": 舊的 id}`，沒有 owner 記 0（`write` 寫入之後，owner 有變的每個資料夾一列）。

### 8.5 IF-URL：`W/services/url_policy.py` 和 `W/views/safe_url_templates.xml`
- **提供者**：L3。
- **使用者**：L8；L7 的範本也會用到。

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
- 只 `_inherit='ir.attachment'`，覆寫 `create(vals_list)` 和 `write(vals)`，**不碰** `unlink`、`check`、`_search`（I-03）。另覆寫 `_index(bin_data, file_type, checksum=None)`：context 帶 `NO_INDEX_CTX_KEY`（`'woow_no_index'`）時回傳 False，否則 super。`WoowDocument.create` 與 `write` 帶這個 context，所以文件的檔案（`datas` 的附件）不產生 `index_content`，不論有沒有安裝 `attachment_indexation`：它的 indexer 不看檔案類型，對任何含 `word/document.xml`、`xl/sharedStrings.xml`、`ppt/slides/slideN.xml`、`content.xml` 成員的壓縮檔整段解壓並建 DOM，而上傳政策只限制壓縮檔的目錄（`_bounded_zip_directory`），不看成員宣告的大小。本模組本身不讀 `index_content`；第二階段的全文檢索 bridge（DEC-13）依賴本模組，載入順序在後，它的 `_index` 排在本模組之前，由它決定讀什麼和上限。另覆寫 `generate_access_token()` 與 `validate_access(access_token)`（見下面「附件不帶 token、不是 public，也沒有 token 能打開它們」）。
- 拒絕（AccessError）的守衛只對非 su 生效；su 適用的是下面兩條：「寫入後的目標」（欄位附件不能被搬移）與「附件不帶 token、不是 public」（不丟例外，把旗標與 token 拿掉）：
  - **create**：`res_model in WOOW_MODELS` 而且符合以下任一條件 → AccessError：`res_field` 有值、`public` 為真、`access_token` 有值（以寫入值，或補上 context 的 `default_*` 與 `ir.default` 之後的有效值判斷）。「有值」依欄位實際存下的內容判斷，不看值是否為真：不是 `None`、不是 `False`，而且 `str(value)` 不是空字串（定義見下面「附件不帶 token、不是 public，也沒有 token 能打開它們」）；`public` 是 Boolean，ORM 存 `bool(value)`，照真值判斷。`False`、`None` 照舊放行。su 不被拒絕，`public` 與 `access_token` 改以 False 建立（下面「寫入端」）。
  - **write**：
    - Q6 查出 self 裡哪些是「本模組 model 的欄位附件」。對這些列，寫入 `GUARDED_WRITE_KEYS = ('raw','datas','db_datas','mimetype','type','url','res_model','res_id','res_field','public','access_token','store_fname')` 任何一鍵 → AccessError。
    - 對本模組 model 的 chatter 附件（res_model 用目前使用者的 ORM 讀取），寫 `public`、`access_token`、`res_model`、`res_id`、`res_field` → AccessError。
    - 把 `res_model` 改成本模組 model、同時設定 `res_field`（有值：同上）→ AccessError。
    - su 不被拒絕：寫入後本模組 model 的列沒有 `public` 與 `access_token`（下面「寫入端」）。
  - `copy()` 走 create，所以一起被擋。`generate_access_token()` 不是：它走 write，而 Odoo 18 的 core 呼叫它的地方有些是 su：`portal/models/mail_message.py` 的 `_portal_message_format`（portal 的訊息格式，`/mail/chatter_fetch` 對傳回的每則訊息的附件，`self.sudo()`）、`mail/models/mail_mail.py` 的 `_prepare_outgoing_list`（信件佇列：信件超過 `base.default_max_email_size` 時，把記錄擁有的附件換成信裡的下載連結，`sudo()`）、`html_editor/controllers/main.py`（上傳圖片時，對不能建立附件的使用者以 `sudo()` 呼叫；修改圖片時以呼叫者自己的環境呼叫），以及 `mail/models/mail_message.py` 的 `create`（內文裡 `data:` 圖片的附件，環境是建立訊息的那一個，來信由 su 處理時就是 su）。`html_editor` 與 `web_editor` 的媒體對話框（插入圖片或文件時）另外以 RPC 呼叫它，環境是使用者自己的，不是 su。拒絕的守衛只對非 su 生效，擋不住 su 的呼叫；下面「附件不帶 token、不是 public，也沒有 token 能打開它們」的 `generate_access_token()` 覆寫是這個方法本身，不論誰以哪個環境呼叫都適用，上面列出的呼叫者（含媒體對話框）都涵蓋，不必一處一處攔；其他寫入 token 的路徑（`create`、`write`）由同一節的「寫入端」處理。
- **寫入後的目標（所有 env，含 su）**：write 帶 `res_model`、`res_id` 或 `res_field`，而寫入後 `res_model` 是本模組 model 時，`_woow_check_field_target` 以 Q6 讀出每列原本的（res_model, res_id, res_field）：寫入後帶 `res_field`（依「有值」的定義：`0` 是欄位 `'0'`，不是沒有欄位）、且與原本的目標不同 → AccessError。core 以 su 把發文者的待送附件改掛到記錄上（`_process_attachments_for_post`，`message_post` 與可指定任何 `model`／`res_id` 的 `message_notify` 都會走到），binary 欄位只建立附件、從不搬移，所以沒有任何途徑能讓文件或資料夾多出一筆欄位附件。
- **附件不帶 token、不是 public，也沒有 token 能打開它們（所有 env，含 su；不依賴誰寫入）**：不變式：`res_model in WOOW_MODELS` 的附件（chatter 附件，以及其他掛在這些 model 上的附件），在任何 create 或 write 之後 `public` 為 False、`access_token` 為空，在所有 env 都成立（常數 `OPEN_KEYS = ('public', 'access_token')`）；沒有 token 能打開它們。**「有值」的定義**（`models/ir_attachment.py` 的 `char_is_set(value)`，`access_token` 與 `res_field` 的每一處判斷都用它）：`value is not None`、`value is not False`，而且 `str(value) != ''`。Char 欄位對每個不是 `None` 或 `False` 的值存 `str(value)`（`fields.py` 的 `_String.convert_to_cache`），所以 `0`、`0.0`、`[]`、`{}`、`()` 存成 `'0'`、`'0.0'`、`'[]'`、`'{}'`、`'()'`，是 token，不是「沒有 token」；只有 `None`、`False` 與空字串什麼都不存。`public` 是 Boolean，ORM 存 `bool(value)`，照真值判斷。su 的重設不看值：寫入值、預設值或寫入的鍵只要帶了 `public` 或 `access_token`，不論值是什麼，本模組 model 的列的兩欄都設成 False。設計決定：連結不帶 token。core 把超限（`base.default_max_email_size`）的 chatter 附件從信裡拿掉、換成下載連結，對本模組記錄也照做，但連結沒有 token：只有已登入、而且能讀該記錄的**內部**使用者，以自己的 session 打得開；其他人都得 404、拿不到檔案，也沒有可用的連結，包含沒有 session 的人與 portal 使用者。portal 成員是信件的對象（HD-10），收得到信、信裡有連結，但檔案不在信裡，連結也打不開。
  - 發行端：`generate_access_token()` 覆寫。self 裡 `res_model in WOOW_MODELS` 的列不寫 token（不分 su、不分呼叫者），在回傳清單的同一個位置放 `False`（token 欄位為空時讀到的值）；其他列交給 super（回傳依 `self` 的順序；沒有這種列時就是 core 的回傳）。`res_model` 以呼叫者自己的 ORM 讀取，不升權：上面的呼叫者清單裡，core 以 su 呼叫的路徑，record 本來就是 su；其他呼叫者讀不到的列，core 自己的 write 也寫不了。
  - 寫入端：`create` 與 `write` 把本模組 model 的列的 `public`、`access_token` 設成 `False`。非 su 的呼叫者照舊被拒絕（上面的守衛）；su 不丟例外（core 以 su 建立與搬移 chatter 的檔案，不能因此失敗），改為從值裡拿掉，不論值是呼叫者寫的、context 的 `default_*` 或 `ir.default` 補的：
    - create：有效的 `res_model`（寫入值或 `default_res_model`）在 `WOOW_MODELS`，而且寫入值，或補上的預設值（context 的 `default_*`、呼叫者與站台的 `ir.default`），帶了 `public` 或 `access_token` 任一鍵（不論值），就以 `public=False`、`access_token=False` 建立。core 為內文引用的檔案以 su 建立並帶剛產生的 token（`_process_attachments_for_post` 依 cid 或檔名；`message_post` 的 `attachments`、`message_notify`，以及 `message_process` 的來信內嵌圖片），這些列因此不存 token；內文網址裡的 token 沒有對應的列，打不開任何東西。
    - write，寫入值的 `res_model` 是本模組 model：core 以 su 把發文者待送的檔案改掛到記錄上（`mail.thread._process_attachments_for_post` 裡的 write 是 su；`message_post`、`message_notify`、`mail.compose.message`（寄件精靈）與 `mail.thread._message_update_content` 都走到它），self 的每一列一併寫入 `public=False`、`access_token=False`，發文者先放在待送檔案上的旗標與 token 不會跟著檔案搬上記錄。
    - write，寫入值帶了 `public` 或 `access_token` 任一鍵（不論值，`0` 與 `[]` 也算）、沒有帶本模組 model 的 `res_model`：讀 self 的 `res_model`（一條 SELECT，連同 ORM 一起預取的欄位，每個預取批次一條；列已在快取時沒有查詢），本模組 model 的列的 `public` 與 `access_token` 一併寫入 False，其他列照寫入值；混合的一組分兩次寫入，只改該改的列。`res_model` 帶了其他 model 或空值時列離開本模組，照 core。
    - 其他寫入（不帶本模組 model 的 `res_model`，也不帶 `public`、`access_token` 兩鍵）不讀任何列，也不增加查詢，與 core 相同。
    - `copy()` 走 create：html editor 的 `modify_image` 以 su 把既有附件複製到記錄上，帶著原列的 `public` 與 token，同樣被拿掉。
  - 取用端：`validate_access(access_token)` 覆寫。先呼叫 super：core 在 token 吻合時以 su 回傳該列，所以讀回傳列的 `res_model` 不需要模組自己升權；`access_token` 有值而且 `res_model in WOOW_MODELS` → `AccessError('Invalid access token')`，與 core 對不吻合的 token 的拒絕相同（`/web/content` 回 404、`/web/image` 回 placeholder，回應不會被標成 public），不論 token 是誰寫入的，已經存入的值也一併失效；沒有帶 token 的呼叫不變。`ir.binary._find_record`（`/web/content`、`/web/image`）對這些列也拒絕任何 token（參數或 query），包含 core 的簽章 token（`limited_field_access_token`），它不經 `validate_access`。
  - 信件：超限時 core 把附件從信裡拿掉，附上 `…/web/content/<id>?download=1&access_token=` 的連結，token 是空的（沒有 `False` 字樣）：不丟例外、信照寄，檔案不在信裡，連結還在。打開連結：已登入、能讀該記錄的內部使用者以自己的 session 取得檔案（失去授權或記錄進回收桶之後是 404）；沒有 session 的收件者得 404；portal 使用者（有帳號、有該資料夾的讀取授權、收得到信）也得 404：core 對沒有 token 的請求，在 portal 帳號要做 `ir.attachment.check('read')`，而它只接受內部使用者。回應都不會被標成 public。其他 model 的附件照 core：有 token、連結可用。
  - 內文裡的嵌入圖片：`mail.message.create` 為 `data:` 圖片建立附件，再用 `access_token` 欄位的值組出網址。對本模組的記錄，發文成功（先前非 su 的發文者會被上面的守衛擋下），但網址是 `/web/image/<id>?access_token=False`；它帶著 token，所以圖片只顯示 placeholder。要在 chatter 放圖片，用附件。
  - 實作不用 `sudo`、`with_user`、`env(su=True)`，也沒有新的 raw SQL（§4.3、§4.4 的白名單不變）；`tools/public_methods.txt` 的 [override] 增列這兩個方法，`check_forbidden` 對覆寫內的 `super().generate_access_token()` 放行（§4.6）。
  - 測試：`test_reg_module_attachments_never_public.py`（發文者放了 public 與 token 的待送檔案：貼文或 `message_notify`（`attachment_ids`）之後旗標與 token 都沒有、訪客帶任何請求（含 `unique=1`、`open.bin`、`download=1`、model／id／field 的形式、`/web/image`）得 404 且回應不會被共用快取保存，撤權與進回收桶之後仍是；對照：其他 model 的公開檔案以同一組請求都得 200；唯讀 follower 的貼文；內嵌圖片的貼文與來信回覆不存 token；複製（含 html editor 的 `modify_image` 以 su 做的複製）；su 的 create 與 write，含 context 的預設值、混合的一組，以及其他 model 的對照；預設值的三種來源（`WAYS`：呼叫者自己存的 `ir.default`、站台的 `ir.default`、context 的 `default_*`），對貼文、內嵌圖片、通知與待送檔案的 su create 都不開啟檔案，非 su 的 create 被拒絕；`0`、`0.0`、`[]`、`{}`、`()` 當作 token 或欄位名稱：su 的 create 與 write 都不存、非 su 的 create 與搬移被拒絕（ORM 與 JSON-RPC `call_kw`）、`False`、`None` 與空字串照舊放行、其他 model 的列照寫入的值存成 `str(value)`；write 的成本：只帶旗標或 token 時讀 `res_model` 是一條 SELECT（以 `cr.sql_log_count` 比較冷、熱快取），帶 model 或什麼都不帶時守衛不讀列）；`test_reg_chatter_attachment_tokens.py`（`/mail/chatter_fetch` 的回傳與資料庫沒有 token、讀者的 session 照常下載、訪客帶任何 token（su 寫入的、簽章的、`unique=1`）得 404、撤權與進回收桶之後仍是 404、超限寄信不存 token 且信照寄，portal 成員的兩項：`test_a_portal_member_gets_neither_the_file_nor_a_working_link`（portal 使用者以任何形式的連結都得 404，讀者以同樣的連結拿到檔案）與 `test_the_mail_of_a_large_file_reaches_a_portal_member_as_a_link_without_the_file`（portal 成員收到信與沒有 token 的連結，檔案不在信裡），以及其他 model 的對照）。

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
    # mail.thread（WoowMailThreadRoute）的同名方法：message_type 是 user_notification（message_notify）、帶附件、model 在 WOOW_MODELS 時，
    #   非 su 的呼叫者要能 write 該記錄（記錄必須存在、id 必須是整數），否則 AccessError；其他訊息類型（message_post）不檢查。
    def _notify_get_recipients(self, message, msg_vals, **kwargs):
        """follower 有帳號、又不是這則訊息明確指定的收件者時，依 Q18 的儲存欄位重驗讀取規則
        （R-F0～R-F5、R-D0～R-D5 的鏡像，不切換身分）；讀不到就不通知。沒有帳號的聯絡人照 core（HD-10）。"""
    # 在 folder 上：write() 對非內部使用者一律 AccessError（HD-07）；
    # message_post 的 record_name、_message_compute_subject、mail.activity 的 res_name、
    # mail.message._get_record_name 都只用資料夾自己的名稱，不帶上層路徑（I-08）。
    # mail.message._compute_display_name（`@api.depends_context('uid')`）：model 是這兩個 model 的訊息，
    # 以 `sudo(False)._filtered_access('read')` 依真正呼叫者判斷，讀不到的人得到 Restricted folder／Restricted document，
    # 而不是儲存在 record_name 的真實名稱（I-08）。
```
- `mail.message` 的標籤：core 的 `Many2one.convert_to_read`、`web_read` 的 `display_name` 與 `read_group` 以 `sudo()` 取 many2one 標籤（uid 不變），`mail.notification` 對 `base.group_user` 開放 read，唯一的規則只管 write，`mail_message_id` 的標籤就是訊息的 `record_name`：任何內部使用者（不必是 Document User、不分公司）因此讀得到受限文件的檔名與資料夾的名稱（只要討論串發過有通知對象的訊息）。標籤改在 `mail.message._compute_display_name` 依真正呼叫者折疊（上述）；作者、收件者與讀得到記錄的人看到的名稱不變，`mail.notification` 的 ACL 與規則不動，`search_read`、`read`、`read_group`、`web_read_group`、`web_search_read` 一次涵蓋。
- `mail.thread._message_route_process`：路由目標是這兩個 model 時，只有 su（fetchmail）或 `base.group_system`（mail gateway 腳本）能處理；其他人經 `message_process` 送信一律 AccessError。
- `ir.binary`（`models/ir_binary.py`）：`_find_record` 對本模組 model 的記錄，以及 `res_model` 屬於本模組 model 的 `ir.attachment` 列（chatter 附件），只要帶 `access_token`（參數或 query）就 MissingError，`/web/content` 回 404、`/web/image` 回 placeholder，回應不會被標成 public；`_record_to_stream` 對 `woow.document.datas` 一律從文件自己的 `attachment_id` 建立串流（Q17 讀出儲存位置；必須是不帶 URL、屬於該文件 `datas` 的 binary 附件，否則 MissingError）。下載、預覽、portal、ZIP 與 `/web/content`、`/web/image` 都經過這裡，其他宣稱同一欄位的附件即使存在也不會被送出。
- `message_ids`、`website_message_ids`、`has_message`（mail.thread 的欄位，在 `woow.document` 與 `woow.document.folder` 上）加 `groups='base.group_user'`：auto_join 的 one2many 在 domain 裡以子查詢直接查 `mail_message`，不經 R-M0，portal 使用者能對自己讀得到的記錄測試內部 log note 的文字；`guard_mixin._search` 與 `_read_group_groupby` 對 domain 與 groupby 的第一段欄位名檢查欄位 groups，portal 因此得到 AccessError，內部使用者不變。
- `ir.binary._record_to_stream` 對文件的 `datas` 回傳 `_DocumentStream`：類型固定為上傳政策對這個檔案提供的類型（`_served_mimetype` 的 base type，由回應補 charset），`ir.binary._get_stream_from` 指派的 `mimetype`（`/web/content`、`/web/image` 的 `?mimetype=`）無效；只有 `INLINE_WHITELIST` 的類型可以 inline，其餘一律 attachment；CSP 固定為 `default-src 'none'; sandbox`。`res_field` 屬於本模組 model 的附件 id（`/web/content/<附件 id>`、`/web/image/<附件 id>`）→ MissingError：文件的檔案只有經由文件這一條路由。
- **ir.attachment write**（非 su 的呼叫者）另依「寫入後的值」判斷：`res_model` 改成本模組 model 時，若該列原本或寫入後帶 `res_field`、`public` 或 `access_token`（寫入值依「有值」的定義，原本的欄位依資料庫是否有值）→ AccessError（兩步驟搬移也擋）。su 不被拒絕，改由「寫入端」把 `public` 與 `access_token` 一併寫入 False；欄位附件不能被搬移的檢查（上面「寫入後的目標」）對 su 也執行。

### 8.7 IF-UNINST：`W/hooks.py`
- **提供者**：L3。
- **使用者**：L0（manifest 的 `uninstall_hook`）、L6（README 的 legal hold 段落）、L12。

```python
def uninstall_hook(env) -> None:
    """env 是 superuser（CA-28）。依序：
    1. 以 active_test=False 查 folder 和 document，只要有任何一筆 legal_hold → UserError(M-71)，訊息只附筆數（ER-30）；
    2. 還有任何 woow.document（含已封存的），而且 uninstall_confirm 不等於 database.uuid → UserError(M-70)。
       database.uuid 用 env['ir.config_parameter'].get_param 讀取（env 是 su，不加 sudo）；
    3. 以 ORM 依序刪除 documents → folders（由深到淺）；
    4. 刪除 PARAM_PREFIX + _PARAM_KEYS 的所有參數。
    只用 ORM，沒有 raw SQL。"""
```

---

## 9. IF-STREAM、IF-ZIP、IF-ROUTE-INT（L4）

### 9.1 IF-STREAM：`W/controllers/streaming.py`（模組層級函式，不是 Controller 類別）
- **提供者**：L4。
- **使用者**：L7、L11、L12。

```python
from ..services.constants import MAX_UPLOAD_BODY        # 99_614_720
def max_upload_body(controller=None) -> int:            # ER-47.2 的 callable 簽名；回傳 MAX_UPLOAD_BODY
    ...

def uniform_404(kind: Literal['page']) -> Response:
    """status 404。
    body 用 eager render：request.env['ir.ui.view']._render_template('woow_document_management.uniform_404_<kind>', {})，
    只依請求的語言而定，不放任何記錄資料、使用者名稱或 csrf_token。
    同一個 kind、同一個語言的 body bytes 完全相同（I-07）。
    標頭：kind='page' 套用 page 標頭；其他 kind → ValueError。"""
def service_busy() -> Response:             # 503，M-45 頁
def limit_page(message_id: Literal['M-41','M-42','M-43','M-44'], status: int, **params) -> Response:
    """ZIP 的上限或忙碌頁面。M-41 用 400，M-42、M-43 用 429，M-44 用 503。"""
def json_response(payload: dict, status: int = 200) -> Response:
    """application/json，套用 no-store 和 nosniff。"""
def apply_common_headers(response: Response, kind: Literal['file', 'page', 'portal', 'json']) -> Response:
    """一律加 Cache-Control: no-store, private 和 X-Content-Type-Options: nosniff。
    file：CSP default-src 'none'; sandbox，加 Cross-Origin-Resource-Policy: same-origin；
    portal：CSP frame-ancestors 'self'（T-PORTAL-18）。"""
def serve(doc, *, inline: bool, channel: Literal['backend', 'portal'],
          event: Literal['download', 'view'] | None) -> Response:
    """呼叫端已經在 doc 所屬的 env 做過權限檢查（內部或 portal）。
    stream = doc.env['ir.binary']._get_stream_from(doc, 'datas', filename=doc.name,
                                                    mimetype=policy._served_mimetype(doc))
    as_attachment = not (inline and policy._inline_allowed(mimetype))
    resp = stream.get_response(as_attachment=…, content_security_policy="default-src 'none'; sandbox")
    檔名經 content_disposition() 處理並去掉 CR/LF；套用 file 標頭；event 有值就記 S1。
    AccessError 或 MissingError → return uniform_404('page')。"""
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
                 channel: Literal['backend', 'portal']) -> Response:
    """documents 和 folders 是呼叫端 env（使用者 env）裡已經 search 過的 recordset。
    鎖（§7.4）：`zip:u:<uid>`；接著取 slot。
    路徑用 folding.zip_segments 加 naming.sanitize_segment 組成。
    讀任何 bytes 之前先用 stored 的 file_size 檢查上限。
    tempfile.SpooledTemporaryFile(max_size=ZIP_SPOOL_MAX_BYTES)，用 wrap_file 加 call_on_close，出錯時在 finally 關檔。
    ZipLimitExceeded → limit_page('M-41' 或 'M-44')；成功時記 zip（detail 是 files 和 bytes）。"""
```

**L0 stub**：見 §3。T-ZIP-27 驗證 `uniform_404()` 回傳的是 Response 實例。

- `create_batch`：每個上傳在讀取之前先 `upload.stream.seek(0)`（請求因並行錯誤被重跑時，框架只會倒回第一個檔案，其餘會讀成空檔案）。`zip_response` 不在列出文件時建立串流：`ZipEntry.open_stream` 是 `_lazy_stream(doc)` 回傳的函式，`build_zip` 寫到那個項目才呼叫 `_get_stream_from`；檔案存在資料庫（`ir_attachment.location = db`）時串流建立就會把整個檔案讀進記憶體，所以大小上限要在讀任何一個檔案之前，以 `file_size` 判斷（§9.1 原有規則）。非 path／data 的串流在這時才丟 MissingError → `uniform_404`。

### 9.2 IF-ZIP：`W/services/zip_builder.py`（純 Python）
- **提供者**：L4。
- **使用者**：L7、L8。

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
- `uniform_404_page`（M-65）、`service_busy_page`（M-45）、`zip_limit_page`（M-41～M-44）。
- 這些都是獨立的最小 HTML，不呼叫 `web.layout`、`portal.portal_layout`，沒有 inline script。

---

## 10. IF-MAIL（原「分享」章節已於 2026-11-13 移除）

連結式分享不是本模組的功能，已於 2026-11-13 移除：原 §10.1～10.3（IF-TOKEN、IF-SHARE、IF-ROUTE-SHARE）連同它們的 model、精靈、路由、範本與設定一併刪除，編號不再使用。

| 規則 | 內容 |
|---|---|
| 取得文件的方式 | 本模組沒有以連結取得文件的功能：沒有帶秘密 token 的網址、沒有 `auth='public'` 的路由。能讀、下載或上傳，只由資料夾的權限（成員授權）和已登入的頁面（後台與 `/my/docs`）決定。 |
| chatter 附件 | 文件與資料夾的 chatter 附件同樣沒有能用的連結：它們不是 public，不帶也不收 access token（§8.6）。超限的 chatter 附件仍照 core 從信裡拿掉、換成下載連結，但連結沒有 token：只有已登入、能讀該記錄的內部使用者打得開；沒有 session 的人與 portal 使用者都得 404（portal 成員收得到信，收不到檔案，連結也打不開，HD-10）。 |

### 10.4 IF-MAIL：`W/data/mail_templates.xml`（`noupdate="1"`，程式永遠不寫入，I-14）
- **提供者**：L8。
- **使用者**：L9（翻譯）。

| xmlid | model | 用到的 context 鍵 | 寄送方 |
|---|---|---|---|
| `woow_document_management.mail_template_woow_document_member_notice` | woow.document.folder | `woow_notice_url`、`woow_notice_note` | L8 的 `notify.wizard.action_send`：內部成員一批（帶後台網址），portal 成員一批（帶 `/my/docs/folder/<id>`）；信裡沒有 token |

- 一律 `send_mail(..., force_send=False)`，收件人在每次寄送時傳入（ER-35）。

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
    每筆包在 savepoint 和 locks.bounded_wait 裡，失敗的只在 log 記下 id 與例外的類別（不寫文字）就跳過。回傳 (done, failed)。
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
- **使用者**：L2（`_cron_check_warn`）、L11、L12。

| xmlid | 方法（在 `woow.document` 上，`@api.model`） | 頻率 | 內容 |
|---|---|---|---|
| `ir_cron_woow_document_purge_trash` | `_cron_purge_trash()` | 每天 | 每批最多刪除 PURGE_BATCH 筆記錄（文件和資料夾都算，不論回收桶資料夾多大）；`ir.cron._notify_progress(done=刪除筆數, remaining=)`；失敗的（含本批快照之後才 commit 的 legal hold、還原）跳過、整批照常 commit，不算進 remaining；同一筆連續失敗 POISON_ALERT_DAYS 天，就對管理員建立 activity（R2b-06）。實作：每日的 cron 會試每一筆保留期已過的記錄，所以本次失敗、而且保留期結束已超過 POISON_ALERT_DAYS 天（`trashed_date < cutoff - POISON_ALERT_DAYS 天`）的記錄，就是已連續失敗那麼多天；每位文件管理員每天最多一個 activity（掛在自己的聯絡人上，只寫筆數，不寫名稱），建立失敗只記 log、不讓 purge 失敗 |
| `ir_cron_woow_document_log_retention` | `_cron_log_retention()` | 每週 | 每批 LOG_RETENTION_BATCH 筆，依 id 由小到大取（最舊的先，取滿批就停），不對剩下的列計數：批滿（剛好 LOG_RETENTION_BATCH 筆）才回報 `remaining` 大於 0，由框架接著跑下一批，不足一批表示已讀到表尾。原因：`create_date` 沒有索引，模型順序是新到舊（`id desc`），過期的列 id 最小，依該順序取批要先倒著走過主鍵上所有未過期的列，剩下的列數則是全表掃描，每一批都這樣 |
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
   - Cache Rule 的條件是 `http.request.uri.path contains "/web/content"`、`"/woow_document/"`、`"/my/docs"` 三者 OR，動作是 bypass；
   - 驗收是 T-E2E-04；
   - 上線時間：在 GM7 的 `/approve` 之前，而且在 G6 之前。
8. **`docs/release/v18.0.1.0.0.md` 欄位**：
   - G0～G5、G1-CA、G1-COV（`sha256:` digest、百分比、各目錄的數字）、G-IP、G-COOKIE、G-CACHE；
   - SC 執行紀錄：T-UPL-32（L2），T-TRASH-10（L6），T-UNINST-03、05（L3），sc_ca*（L11）。
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
- tag 一律是 `@tagged('post_install', '-at_install', 'woow_docmgmt', 'woow_docmgmt_<core|portal|files>')`。
- `test_perf.py` 用 `('woow_docmgmt_perf', '-standard')`，只在指定 tag 時跑（ER-42）。
- docstring 第一行寫正規測試 ID，有對應的 T-SEC 時一起寫，例如 `T-ACL-46 / T-SEC-21`。
- U 類測試放在 `W/tests/`，類別繼承 `odoo.tests.BaseCase`。

### 17.3 沙盒指令與 EXPECTED_MIN
- `SLOT=<lane key> dev/sandbox_test.sh [tags]` 包裝 沙盒工具（`$WOOW_SBX`）：copydb 基底 DB → test → 解析測試數 → 比對 EXPECTED_MIN → dropdb。輸出 digest、總數和各檔數。
- 只裝一個模組（ER-42 第 4 點）。各 lane 都不修改 沙盒工具（`$WOOW_SBX`）（ER-47.8）。
- `W/tests/EXPECTED_MIN.json` 由 L11 維護，屬於 always_human。格式：
  ```json
  {"version": 1, "batches": {"core": 0, "portal": 0, "files": 0},
   "files": {"test_core_models.py": 0}}
  ```
  任何一項低於下限，或總數是 0，就失敗。
- 需要真實 HTTP 的 SC 交給 `dev/sc/run_sc.sh` 執行（ER-47.8）：讀 `$WOOW_SBX`，不寫預設值，也不出現 namespace 或主機名稱。

---

## 18. lane → 介面矩陣（P＝提供，C＝使用；L11 依 ER-47.13 唯讀使用全部介面）

| 介面 | L0 | L1 | L2 | L3 | L4 | L6 | L7 | L8 | L9 | L10 | L11 | L12 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| IF-SKEL | P | C | C | C | C | C | C | C | C | ·（間接） | C | C |
| IF-STATIC | P | C | C | C | C | C | C | · | · | · | C | C |
| IF-CONST | ·（第 0 天代寫） | C | C | P | C* | C* | C* | C | · | C | C | · |
| IF-SET | · | P | C | C | C | C | C | C | · | · | C | C |
| IF-IP | · | P | · | C | · | · | · | · | · | · | C | C |
| IF-LOG | · | C | · | P | C | C | C | C | · | · | C | · |
| IF-UPL | · | · | P | C | C | · | C | ·* | · | · | C | · |
| IF-QUOTA | · | · | P | C | · | C | · | · | · | · | C | · |
| IF-NAME | · | · | P | C | C | · | ·* | · | · | · | C | · |
| IF-LOCK | · | · | P | · | C | C* | · | · | · | · | C | · |
| IF-MODEL | · | · | · | P | C | C | C | C | · | C | C | · |
| IF-FOLD | · | · | · | P | C | · | C | · | · | · | C | · |
| IF-URL | · | · | · | P | · | · | ·* | C | · | · | C | · |
| IF-GUARD | · | · | · | P | · | · | · | · | · | · | C | · |
| IF-UNINST | C | · | · | P | · | · | · | · | · | · | C | C* |
| IF-STREAM | · | · | · | · | P | · | C | · | · | · | C | · |
| IF-ZIP | · | · | · | · | P | · | C | C | · | · | C | · |
| IF-ROUTE-INT | · | · | · | · | P | · | · | C | · | · | C | C |
| IF-MAIL | · | · | · | · | · | · | · | P | C | · | C | · |
| IF-TRASH | · | · | · | · | · | P | · | C | · | · | C | · |
| IF-CRON | · | · | C* | · | · | P | · | · | · | · | C | · |
| IF-PORTAL | · | · | · | · | · | · | P | · | C | · | C | C |
| IF-UI | · | C*（選單） | · | · | · | · | · | P | C | · | C | · |
| IF-TEST | · | ·* | ·* | C | C | C | C | C | C | C | P | C |
| IF-RB | · | · | · | P（IF-RB-1～5） | · | · | · | · | · | P | C | C |
| IF-REL | · | · | · | · | · | · | · | · | · | · | C | P |
| PR-01 | P | C | C | C | C | C | C | C | · | C | C | · |
| PR-02 | P（骨架） | · | · | · | · | · | · | · | · | · | P | · |
| PR-14 | · | · | · | P | · | · | · | · | · | C | C | · |

- 標 `*` 的是本文建議補進 lanes.json `depends_on_interfaces` 的依賴（實際會用到，但 lanes.json 沒有列）：
  - L4、L6、L7 加 IF-CONST；
  - L6 加 IF-LOCK（`bounded_wait`）；
  - L7 加 IF-NAME、IF-URL（ZIP 檔名、範本裡的網址）；
  - L1 和 L2 加 IF-TEST；
  - L2 加 IF-CRON（被 cron 呼叫）；
  - L12 加 IF-UNINST（README 和 runbook）。
- L8 依 ER-47.13 加上 IF-LOG 和 IF-CONST，矩陣已經納入。

---

## 19. 文件之間的衝突和採用的解法（設計勘誤 和 IDS 優先）

| # | 衝突 | 解法 |
|---|---|---|
| 2 | arch §7.1 寫 `max_content_length`＝callable min(95 MiB, 設定)；§7.2 寫 ×檔數；sec R8 要求 callable；sec R1 要求 route 參數都是字面量；lanes L7 寫 `=streaming.max_upload_body` | 採 ER-03 和 ER-47.2：decorator 寫整數運算式 `95 * 1024 * 1024`；check_routes R1 和 R8 接受「值等於 99,614,720 的整數常數運算式」，或名稱是 `max_upload_body` 的 callable；`streaming.max_upload_body(controller=None)` 保留給測試比對（**IC-R03**） |
| 3 | arch §11、§16 的內部上限預設 50 MiB | 改成 95（ER-03） |
| 5 | sec R11 允許 `_legacy_gone()`；R3 和 R6 有 legacy 例外 | 刪除（ER-27） |
| 7 | arch §10.2 用 Python 的 `hash()` 產生鎖的鍵 | 改用 sha256 的 `advisory_key`（ER-24） |
| 8 | ER-24 寫 `SET LOCAL lock_timeout` 再 `TO DEFAULT`；ER-47.3 改成 `set_config(...)`，沒寫還原 | 用 ER-47.3 的 set_config，再用同一個語句還原原本的值，四個語句都列進 Q4（**IC-R08**） |
| 10 | ER-47.4 規定 Q3 只有兩個位置；arch §3.2、§3.5 卻要求 access 和 tag 的運算式唯一索引 | 這兩個（以及 attachment_id）改在 `init()` 用 `odoo.tools.sql.create_unique_index` 建立（`tools/sql.py` L563），不寫 raw SQL；Q3 維持兩個位置（**IC-R07**） |
| 11 | arch §13.4 的 purge 用 `SKIP LOCKED`，但 Q1～Q6 沒有這一條 SQL；IDS 的 T-TRASH-10 又要求「跳過被鎖住的列」 | 不新增 SQL。改用 `locks.bounded_wait(cr, ms)`：在每一筆的 savepoint 裡短暫設定 lock_timeout，逾時就跳過。語句沿用 Q4 的 set_config（**IC-R09**，L2 提供、L6 使用） |
| 12 | arch §14 有 22 種事件、含 legacy channel | 改成 12 種、4 個 channel（ER-37；連結式分享的 9 種事件與 share channel 已於 2026-11-13 移除） |
| 13 | arch §1.3、§4.1 和 L10 細部規格 §4.1 的註腳都說 admin 透過 groups.xml 的 users 欄位加入 | 改用 `data/admin_assignment.xml`，是 noupdate 的 `<function>`（ER-38、ER-45.5、PR-14） |
| 14 | arch §25 的 HD-10，以及 sec R2b-01「HD-10 第二階段才適用」 | 採 ER-15 的改寫版，並顯示提醒 |
| 15 | arch §8.7 的寄信用 `force_send=True` | 改成 `force_send=False`（ER-35） |
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
| 28 | lanes L7 說有 6 個 entry point；ER-47.5 說有 5 個 decorated 函式 | 兩者一致：5 個 decorated 函式，加上 `_prepare_home_portal_values`；「文件頁加下載」共用一個函式，用固定的路徑結尾分辨（**IC-R14**） |
| 30 | sec §8 的 check_public_methods 只允許覆寫 create、write、unlink、copy；但 `set_values`、`message_post`、`message_subscribe`、`init`、精靈的 `default_get` 都必須覆寫 | public_methods.txt 增加 [override] 區段（§4.9），規則是「必須呼叫 super，或直接丟錯」（**IC-R10**） |
| 32 | 舊 ic §10 說 CA-23 不成立就「擋發版」 | 改成 L2 從 INLINE_WHITELIST 拿掉 `application/pdf`（ER-47.14） |
| 33 | arch §1.2 寫 private repo 和 README.rst | 改成 public repo 和 README.md（ER-01） |
| 34 | IDS §2.2 寫 test_static_checks 涵蓋 T-STAT-01～13、15～25 | 依 ER-47.6：01～13、16～25；T-STAT-15 放在 test_core_assumptions（PR-02） |
| 35 | sec §10 列 28 支 regression_tests | 改成 36 支（ER-23）；分享的 3 支移除後是 33 支 |
| 37 | constants.py 歸 L3，但其他 lane 第 0 天就要用到上限值 | L0 第 0 天依 §5 寫進最終值，之後由 L3 擁有（**IC-R02**） |
| 38 | ER-32 的 stub 表沒有涵蓋守衛、hook 等檔案 | §3 補上，全部 fail closed（**IC-R01**） |

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
| §4.8 | 刪除（搬遷） |
| §5 | §1.2 R-RESP |
| §6 | §4.3、§4.4 |
| §7 | §17.2、§17.3 |
| §8 | §1.4、§16 |
| §9 | DEC-02、03、05、06 作廢；DEC-15、18 依 ER-02 改寫（不再有搬遷模組的介面） |
| §10（Q-A） | 作廢；改成本文 §19 |


---

## 21. 審查勘誤（contract verifier，2026-11-07；綁定，優先於本文前面各節）

以下 B3、B5～B7 的取代文字直接取代所指章節的原文；實作以本節為準。B1、B2、B4 隨連結式分享移除，編號不再使用。
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

## B5：單一擁有權
§3 最後一列「controllers（L4、L7）」的 stub 行為，換成：
> 只有類別，沒有任何 route。`tools/routes.lock.json` 在第 0 天由 L0 依本文 §9.3、§12 寫好全部 9 列的最終內容，之後各 lane 不修改。check_routes R12 在非 `--g1` 模式檢查「抽到的每個 route 都和 lock 檔的某一列完全相同」（子集）；`--g1` 模式檢查兩者相等。route 要改，就送契約變更請求給 L0（IC-R12）。

§19 #20 的解法，換成上面這段的最後兩句。

§1.1 第 2 點，換成：
> 要改本文的任何簽名、xmlid、常數或設定鍵，都要對擁有的 lane 發「契約變更請求」。`docs/CONTRACTS.md` 只有 L0 能改：擁有 lane 合併時，在 PR 描述的 `### CONTRACTS` 區塊交出變更紀錄文字，由 L0 合併進去。

§7.1 INLINE_WHITELIST 的註解，換成：
> `# CA-23 不成立時，由 L2 拿掉 'application/pdf'，變更紀錄交 L0 記入 docs/CONTRACTS.md（ER-47.14）`

lanes L11 deliverable「記入 docs/CONTRACTS.md 變更請求」照這個方式解讀。

## B6：補上 stub
§3 表格補以下幾列，或擴充原列：
| `controllers/streaming.py`（L4） | 原列再加：`apply_common_headers(response, kind)` 回傳加上最終標頭的 response（純函式，第 0 天就寫最終邏輯，不標 WOOW-STUB 也可以） | ER-32 |
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
3. IDS §2.2 寫 `static/tests/tours/**` 歸 L8（T-CA-21 的 tour 例外歸 L11）。ic 和 lanes 再把 tours/test_suites/** 切給 L11（T-ESC tour）。這和 IDS 的位階不一致，建議在 §19 補一列說明，並送 IDS 勘誤。
4. L3 的 `_log_event` 正常路徑需要 L1 的 `_audit_ip_key`（ER-32 規定第 0 天的 stub 丟 NotImplementedError），所以 L3 要等 L1 合併後 rebase 才能把測試跑綠。合併順序本來就是 L1 先，不擋，但建議第 0 天先放 `hmac_ip`／`truncate_ip` 的最終純函式。



---

# 第 2 部分：代號表（淨化版）

## 1. Lane

### 1.1 正規 lane key

| Lx | key | 名稱 | 和 lanes.json 的差異 |
|---|---|---|---|
| L0 | `ci-sweep` | repo 骨架、第 0 天介面 stub、static tools、CI、sweep 設定、私有 target、推送前檢查 | 加 stub（ER-32）、推送前檢查（ER-01）；target 改 56 類加 review_units（ER-07） |
| L1 | `settings-ip` | 設定（S3）和真實客戶 IP | 內部上限 95 MiB（ER-03） |
| L2 | `upload-quota` | 上傳政策、配額、naming、mime_map、**advisory lock**（`services/locks.py`） | 加 locks.py 和 test_locks.py（ER-24） |
| L3 | `core-models-acl` | 核心 model、權限、附件守衛、mail_thread_guard（R2b-01）、log 的 schema、uninstall_hook | 加 R2b-01 和 test_mail_thread_guard.py（ER-20）、legal hold 拒絕解除安裝（ER-30）、admin noupdate（ER-38）、移除 `policy_flag` |
| L4 | `internal-routes-zip` | streaming（Response 契約）、內部路由、ZIP、原生路由測試 | 加 test_native_routes.py、`service_busy`（ER-24、ER-29、ER-36） |
| L6 | `trash-audit-cron` | 回收桶、法律保留、稽核行為、所有 cron | 加 poison record（R2b-06）、資料夾還原需要管理（ER-13） |
| L7 | `portal` | `/my/docs` 瀏覽、下載、原子上傳、麵包屑折疊 | 請求上限固定 95 MiB（ER-03） |
| L8 | `backend-ui` | 後台 UI、附件總覽、加入資料夾、搬移和通知精靈 | 加 HD-10 提醒（ER-15）；通知信改成佇列寄送 |
| L9 | `i18n-zh-tw` | zh_TW 翻譯和用語 | 刪除 LU 的 i18n；「G-5」改稱 T-I18N-01／02 |
| L10 | `site-rebuild`（repo 外） | 一次性重建腳本（repo 外的重建腳本） | **取代 `migration`**（ER-06） |
| L11 | `test-suites` | 共用 fixture、獨立的安全測試、CA-01～CA-35、覆蓋率、SC 執行器 | 加 CA-34、CA-35、`dev/coverage.sh`（ER-09） |
| L12 | `paas-release` | 打包、平台關卡（G-IP、G-COOKIE、G-CACHE）、runbook、發版紀錄、殘留檢查 | 刪除搬遷時序；加 G-CACHE（ER-10）、殘留檢查（ER-34）；「G-7」改名 G-SIM |

舊 key 的別名只用在換算，新文件不准再寫：
- `migration` → `site-rebuild`（repo 外）

### 1.2 舊編號 → 正規 lane

| 出處 | 舊代號 | 正規 lane |
|---|---|---|
| arch §24（以及 sec §1 的 lane 欄） | L1 | L3 `core-models-acl` |
| 同上 | L2 | L1 `settings-ip` ＋ L2 `upload-quota`（log 歸 L3） |
| 同上 | L3 | L8 `backend-ui` |
| 同上 | L5 | L7 `portal` |
| 同上 | L6 | L4 `internal-routes-zip` |
| 同上 | L7 | L6 `trash-audit-cron` |
| 同上 | L8 | L10 `site-rebuild`（repo 外）（範圍改成 ER-06；legacy 部分刪除） |
| 同上 | L9 | L0 `ci-sweep` ＋ L11 `test-suites` |
| 同上 | L10 | L12 `paas-release` |
| lanes.json | L0～L12 | 號碼相同；L10 改名，見 §1.1（L5 已移除） |

### 1.3 合併順序（依賴的意思是「合併」，不是「開工」；ER-32）
1. L0 合併
2. L1
3. L2
4. L3
5. L4
6. L6、L7（可以並行）
7. L8
8. L10
9. L9
10. L11 收尾
11. L12

L0 合併之後，L1～L8 和 L11 就可以開工。L10 要等 L3 合併後才能在沙盒跑測試。

### 1.4 各 lane 擁有的檔案（只列和 lanes.json、ic §3.1 不同的地方；`W/`＝`woow_document_management/`）

| lane | 新增 | 刪除 |
|---|---|---|
| L0 | `ops/prepush-check.sh`（repo 外）、`tools/sql_allowlist.yaml`（現有的代號：Q2～Q8、Q17～Q21）、`tools/cleanroom_forbidden.txt`（只放 ER-33 的內容） | LU 和 MS 的 manifest 與 `__init__.py`；plat 的 sh target 退役改成 G3 交付 |
| L1 | — | — |
| L2 | `W/services/locks.py`、`W/tests/test_locks.py` | — |
| L3 | `W/tests/test_mail_thread_guard.py`、`W/data/admin_assignment.xml`、`dev/sc/sc_uninstall.py` | `policy_flag` 欄位 |
| L4 | `W/tests/test_native_routes.py` | — |
| L6 | `dev/sc/sc_purge_skip_locked.py` | — |
| L7 | — | — |
| L8 | — | — |
| L9 | — | `LU/i18n/*` |
| L10 | 重建腳本（repo 外） | `MS/**` 全部 |
| L11 | `dev/coverage.sh`、`dev/sc/sc_ca*.py`（CA-06、23、24、28）、`W/tests/EXPECTED_MIN.json` | `dev/sandbox_test.sh` 裡三個模組的部分 |
| L12 | `ops/residual-check/**`（repo 外）、G-CACHE 的 ruleset 和套用腳本 | 舊的搬遷 runbook；`cloud-addons.list` 裡的 legacy_urls |

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
| T-MC | 多公司 | test_multicompany.py |
| T-SET | 設定、URL 政策、客戶 IP | test_settings_bounds.py、test_url_policy.py、test_client_ip.py |
| T-ESC | 輸出跳脫 | test_escaping.py |
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
| 07 | 廢止（T-MC-04 隨分享一起移除） | — |
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
| 24～41 | 廢止（連結式分享已移除） | — |
| 42 | T-PORTAL-18 | L7 |
| 43～47 | 廢止（連結式分享已移除） | — |
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
| 76 | T-ACL-17 | L3 |
| 77 | T-STAT-05 | L0 |
| 78 | T-UPL-30 | L2 |
| 79 | T-ACL-02 | L3 |
| 80 | T-ACL-52 | L3 |
| 81（新） | T-ZIP-27，T-STAT-23（Response 不 raise，ER-29） | L4、L0 |
| 82（新） | T-UNINST-08（有 legal hold 就拒絕解除安裝，ER-30） | L3 |
| 83（新） | T-ACL-53（portal hash／pid／token fallback，ER-20） | L3 |
| 84（新） | 廢止（連結式分享已移除） | — |
| 85（新） | T-TRASH-05（資料夾還原需要管理，ER-13） | L6 |
| 86（新） | T-UPL-30、31，T-STAT-22（鎖的鍵、lock_timeout、503，ER-24） | L2、L0 |
| 87（新） | T-RB-01～10（重建只給檢視、冪等、只印 id，ER-06） | L10 |
| 88（新） | 廢止（連結式分享已移除） | — |
| 89（新） | T-ACL-54（admin noupdate，ER-38） | L3 |
| 90（新） | T-E2E-04（G-CACHE 紀錄，ER-10） | L12 |

sweep 類別對 T-SEC 的對照是 sec §3，套用 設計勘誤 ER-23 第 3 點的修改。

---

## 5. R2b → lane

| R2b | 嚴重度 | 正規 lane | 測試 | 設計勘誤 |
|---|---|---|---|---|
| R2b-01 | high | L3 `core-models-acl` | T-ACL-46、47、53（T-SEC-21、22、83） | ER-20 |
| R2b-02 | medium | 不適用（連結式分享已移除；static 規則仍在 L0） | T-STAT-21 | ER-22 |
| R2b-03 | medium | 不適用（連結式分享已移除；static 規則仍在 L0） | T-STAT-21 | ER-39 |
| R2b-04 | medium | L4 `internal-routes-zip`（殘餘）；HD-12 文字由 L3 提供；CA-35 由 L11 撰寫 | T-ZIP-26，T-CA-35（T-SEC-68） | ER-36（搬遷部分不適用） |
| R2b-05 | medium | L2 `upload-quota`（使用者是 L4） | T-UPL-13、30、31，T-STAT-22（T-SEC-54、78、86） | ER-24 |
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
| 2026-11-11 | G4 修正 | **拒絕紀錄容量**：任何人都能造成的拒絕紀錄（denied bucket、來源計數列）原本沒有數量上限。現在每分鐘新增的列最多 `MAX_DENIED_ROWS_PER_MIN`（1024）列，由 `woow.document.denied.budget` 的分鐘列配給（`_take_slot`，Q14）：新的來源計數列與 denied bucket 各占一格，已存在的列不占，`share_unlock_failed` 不占（`UNBUDGETED_EVENTS`）。denied bucket 不寫 `share_id`，剩下的容量不透露分享是否存在；用完時新來源的第一次拒絕回 429、不寫列，有效連結與已授權的 session 不受影響。測試：`test_audit_log.py`、`test_share_routes.py`。 |
| 2026-11-11 | G4 修正 | **來源限流計數**：來源＝`client_ip.source_key`（IPv4 原位址；IPv6 取 /64；IPv4-mapped 取其 IPv4），`ip_hash` 與計數列都用來源的 HMAC。跨分享的計數放在 `woow.document.denied.counter` 的（事件、來源、分鐘）列（Q13），在請求交易內鎖定；新列沒有容量時回 429、不寫列。被拒絕的連結請求一律不帶 `share_id`，只有計入的錯誤密碼（`share_unlock_failed`）保留，回應與容量都不依分享是否存在。測試：`test_audit_log.py`、`test_share_routes.py`。 |
| 2026-11-11 | G4 修正 | **密碼解鎖與分享列屏障**：`share_unlock` 與下載先以請求快照判斷已用完的上限（舊快照只會少算），不取鎖、不寫列；要比對密碼或放行下載時才取 `share:<id>` 鎖與 share 列的 `FOR NO KEY UPDATE`（Q12，不寫列版本），之後重新計數。只有計入的錯誤密碼或限次連結的下載才寫新的列版本，所以快照早於它們的並行請求會 serialization_failure 並重試，沒有計入的嘗試不會讓擁有者的撤銷或編輯失敗。鎖順序：`share:<id>` → share 列 → 來源計數列 → 事件 bucket。測試：`test_share_routes.py`；SC：T-SHARE-44、45、46。 |
| 2026-11-11 | G4 修正 | **User-Agent 清理**（補充 §8.4 `user_agent` 一點）：`_log_event` 寫入前先移除 user agent 的控制字元（含 NUL）並截斷到 256 字元（`USER_AGENT_SIZE`），ok 與 denied 兩條路都套用；含 NUL 的值原樣寫入會使寫入失敗、整個請求回滾。測試：`test_audit_log.py`。 |
| 2026-11-12 | 審查修正 | **附件與串流**：`ir.attachment.write` 依寫入後的值判斷，帶 `res_field`、`public` 或 `access_token` 的附件不能分兩步改掛到本模組 model；經 `_process_attachments_for_post` 加入討論的附件不能是欄位附件（Q17）；`ir.binary._record_to_stream` 只串流文件記錄的那一個 binary 附件，`_find_record` 對本模組記錄拒絕 `access_token`。測試：`test_attachment_guard.py`、`test_internal_routes.py`。 |
| 2026-11-12 | 審查修正 | **mail**：`message_new` 一律拒絕；`_message_route_process` 路由到文件或資料夾時只接受 su 或系統管理員；portal 只能移除自己的 follower；follower 讀不到記錄後不再收到通知（Q18，HD-10）；資料夾的 record_name、信件主旨、活動名稱只用資料夾自己的名稱。測試：`test_mail_thread_guard.py`。 |
| 2026-11-12 | 審查修正 | **名稱與排序**：同層同名回 M-02（Q19，不論另一個資料夾是否看得到）；別的 model 依指向資料夾的 many2one 排序或分組時，非文件管理員改依資料夾 id；文件的 display_name 依呼叫者的 record rule 折疊為「Restricted document」；guard mixin 的 `_search` 與 `_read_group_groupby` 檢查欄位 groups；`/my/docs` 的直接分享改走 `_woow_portal_direct_documents`（Q20）。測試：`test_core_models.py`、`test_display_name_folding.py`、`test_record_rules.py`。 |
| 2026-11-12 | 審查修正 | **配額、分享信與回收桶**：一般上傳一律計入檔案大小，只有加入資料夾精靈抵扣已存的相同內容，百分比只給文件管理員；佇列中的分享信，其 mail.message 副本的 body 清空，連結只留在 mail.mail；purge cron 每批最多 `PURGE_BATCH` 筆，已隨上層刪除的記錄略過。測試：`test_quota.py`、`test_mail_share.py`、`test_trash_lifecycle.py`。 |
| 2026-11-12 | 審查修正 | **稽核與上傳**：`access_change` 每個資料夾一列、只列該資料夾的成員列 id，detail 的 id 清單最多 500 個；ZIP64 end record 與 central directory 的標頭數也套用 4096 筆、1 MiB 的上限，先於 ZipFile 拒絕；信任代理與用戶端 IP 標頭的設定加上說明。測試：`test_audit_log.py`、`test_upload_policy.py`。 |
| 2026-11-12 | 審查修正 | **回收桶與 legal hold**：分批 purge 的每個根資料夾在讀任何 hold 之前先鎖住自己的列（Q21，`FOR NO KEY UPDATE`，只鎖不寫列版本），本批快照之後 commit 的 hold、還原或搬移讓該根 serialization_failure、整個根都不刪；文件的 hold 與刪除是同一列。這些錯誤與並行死結都只讓該根或該文件算 failed、跳過，整批照常 commit。測試：`test_trash_lifecycle.py`。 |
| 2026-11-12 | 審查修正 | **通知**：follower 的讀取權重驗先套公司規則（R-D0、R-F0 沒有 groups），portal 分支另要求 `base.group_portal`（R-D5、R-F5）；只在其他公司的 portal follower 與直接分享對象不再收到該記錄的通知。測試：`test_mail_thread_guard.py`。 |
| 2026-11-12 | 審查修正（第二輪） | **purge 屏障**：`_woow_lock_purge_root`（Q21）依 id 順序鎖住根與子樹每個資料夾的列（`active_test=False`），本批快照之後改到其中任一列的 hold、還原或搬移，該根就 serialization_failure、整個根都不刪；`subtree_legal_hold` 與 `has_restricted_descendant` 以 `with_context(active_test=False).child_ids` 計算，`init()` 每次安裝與更新時重算。測試：`test_trash_lifecycle.py`。 |
| 2026-11-12 | 審查修正（第二輪） | **分享 id 路由與附件**：`_share_entry` 先讀 session 授權，沒有就直接 `_denied`，不存在、已撤銷與有效的 id 查詢數相同；`ir.attachment.write` 在所有 env（含 su）都拒絕讓附件寫入後成為本模組記錄的另一筆欄位附件，`_record_to_stream` 一律從文件自己的 `attachment_id` 串流（Q17 `_woow_document_file_row`）。測試：`test_share_routes.py`、`test_attachment_guard.py`。 |
| 2026-11-12 | 審查修正（第三輪） | **失敗密碼 bucket 的鍵**：`woow.document.log` 新增 `link_ref`（純整數，不是外鍵）：舊的唯一索引以 `share_id` 為鍵，刪除分享連結時外鍵清成 NULL，會讓兩個 bucket 的鍵相同而刪除失敗。現在鍵是（事件、來源、`COALESCE(link_ref, 0)`、分鐘），只有 `UNBUDGETED_EVENTS` 的 denied 列寫入 `link_ref`；索引改名 `woow_document_log_denied_link_bucket`，`init()` 先把舊 denied 列的 `link_ref` 設成各自的 `share_id` 再換索引。刪舊連結與 purge 各自接住單一記錄的 `IntegrityError`（只記 id）。測試：`test_audit_log.py`、`test_trash_lifecycle.py`。 |
| 2026-11-12 | 審查修正（第三輪） | **取槽的第二條連線**：取槽（`_take_slot`）借第二條連線：取不到（`PoolError`，或資料庫拒絕連線的 `OperationalError`）視為沒有槽，走既有的「容量用完」路徑（429、不寫列）。每個行程同時開著的取槽交易最多 `SLOT_CONNECTIONS`（`max(1, db_maxconn // 32)`）個，其餘最多等 `SLOT_WAIT`（0.5 秒），逾時同樣沒有槽，請求自己的連線不會被取槽用完；部署建議見 SECURITY.md。測試：`test_audit_log.py`；SC：T-SHARE-47。 |
| 2026-11-12 | 審查修正（第四輪） | **分享檔案路由的文件 id**：`share_file` 的 `document_id` 超出資料庫 integer 範圍（含 0）時視為分享範圍裡找不到：不查詢，和其他找不到的 id 一樣走 `_denied`（404，計入來源計數）。測試：`test_share_routes.py`。 |
| 2026-11-12 | 審查修正（第四輪） | **圖片像素上限**：`upload_policy._validate` 把 Pillow 的 `DecompressionBombError` 與超過一倍上限的警告都對應成 `WoowUploadRejected('pixels')`（400 JSON），像素數的比對移到 `with` 之後；讀不懂的圖片仍是 `content`。測試：`test_upload_policy.py`、`test_internal_routes.py`、`test_portal_routes.py`。 |
| 2026-11-12 | 審查修正（第五輪） | **聊天欄位與精靈列**：`message_ids`、`website_message_ids`、`has_message` 在 `woow.document` 與 `woow.document.folder` 加 `groups='base.group_user'`（auto_join 的 one2many 在 domain 不經 R-M0）。六個精靈 model 各加一條 `create_uid = user.id` 的 ir.rule（R-W1～R-W6，登錄在 `tools/security_matrix.yaml`），別人不能讀或改管理員正要確認的那一列。測試：`test_mail_thread_guard.py`、`test_reg_wizard_owner_scope.py`。 |
| 2026-11-12 | 審查修正（第五輪） | **資料夾與沒有公司的記錄**：資料夾 `write` 的 `company_id` 只有文件管理員能改，`create` 的 `company_id` 為空也只有文件管理員能建（T-MC-05）；`parent_id` 清空要過和建立根資料夾一樣的政策（M-24）。`_log_event` 的 `company_id` 接受 False（記成空公司，R-L0）；purge cron 對每一筆接住任何例外（只記 id 與類別），並實作 R2b-06 的管理員提醒（`POISON_ALERT_DAYS`）。測試：`test_multicompany.py`、`test_audit_log.py`、`test_trash_lifecycle.py`。 |
| 2026-11-12 | 審查修正（第五輪） | **ZIP**：`naming.Deduper` 一次一個名稱、每個（主檔名、副檔名）記住下一個數字（`dedupe` 的簽章與輸出不變）；`ZipEntry.open_stream` 在寫到該項目時才讀（`_lazy_stream`），`zip_response` 不在列出文件時建立串流。測試：`test_naming.py`、`test_zip_builder.py`。 |
| 2026-11-12 | 審查修正（第五輪） | **分享**：建立者已被刪除（`create_uid` 為空）的連結不再解析；公開的 `share_open`／`share_access`／`share_download` 以請求的環境記錄（訪客，不是建立者），`_record_download` 新增必填參數 `log_env`；`share_close` 只在確實移除授權時才寫 session；`_scope_documents` 以 `_woow_filter_rank` 一次查出 manage 權限。測試：`test_share_routes.py`。 |
| 2026-11-12 | 審查修正（第五輪） | **設定與上傳**：`res.config.settings.default_get` 對非系統管理員拿掉 `woow_doc_*` 欄位；`document.create` 對非 su 套用每次請求的檔案數上限（`max_files_per_upload`）；PNG、JPEG、GIF 的標頭結構在 Pillow 開啟前先計數（`IMAGE_MAX_STRUCTURES`、`IMAGE_MAX_JUNK`）；`/web/content`、`/web/image` 對文件的 `datas` 固定使用上傳政策的類型，非 inline 類型一律 attachment，CSP 固定含 `sandbox`。測試：`test_rpc_exposure.py`、`test_reg_create_batch_cap.py`、`test_upload_policy.py`、`test_internal_routes.py`。 |
| 2026-11-12 | 審查修正（第五輪） | **配額**：一次呼叫的檔案在全部驗證之後一起檢查（`_check_many`），已 commit 的用量以獨立連線在取得配額鎖之後讀（Q10 移除），同一個交易的多次呼叫共用 `QuotaLedger`；每個帳號有自己的 `quota:u:<uid>` 鎖（rank 3，先於 `quota`）。SC：T-UPL-32（`dev/sc/sc_quota_contention.py`）。測試：`test_quota.py`。 |
| 2026-11-12 | 審查修正（第五輪） | **拒絕計數**：`ATOMIC_EVENTS`（`share_access`）的拒絕改走 `_refuse`：來源計數列（Q22）與 audit bucket（Q23）各由一條語句在自己的 READ COMMITTED 短交易更新並立即 commit，同一來源的並行請求不互相等待、不 serialization_failure，計入的恰好是上限筆；新列在同一個短交易取容量槽。錯誤密碼的比對（`share_unlock_failed`）仍在請求交易以鎖定的來源計數列與 share 列屏障序列化。SC：T-SHARE-48（`dev/sc/sc_denied_one_source_together.py`）。測試：`test_audit_log.py`、`test_share_routes.py`。 |
| 2026-11-12 | 審查修正（第六輪） | **ZIP 命名**：`Deduper` 記住的鍵改為（數字的位數、為該位數的數字裁短後的主檔名、副檔名），後兩者以 `casefold()` 比對，使裁短後相同的長主檔名共用同一個計數；輸出與先前版本逐字相同。測試：`test_naming.py`、`test_zip_builder.py`。 |
| 2026-11-12 | 審查修正（第六輪） | **PNG chunk**：`_png_structure_ok` 走過整個檔案到 IEND：非影像資料的 chunk 最多 256 個（`PNG_MAX_CHUNKS`）、`iCCP` 1 個（`PNG_MAX_PROFILES`）、`zTXt` 加 `iTXt` 16 個（`PNG_MAX_COMPRESSED_TEXTS`），影像資料的 chunk 超過 4096 個之後平均至少 256 位元組（`PNG_FREE_IMAGE_CHUNKS`、`PNG_IMAGE_CHUNK_BYTES`）；超過 → `'content'`，不呼叫 `Image.open`。測試：`test_upload_policy.py`。 |
| 2026-11-12 | 審查修正（第六輪） | **測試的獨立性與文件**：`TestPurgeRacesWithLegalHold._purge_batch` 不送出 `_woow_alert_stuck_trash` 並檢查資料庫沒有多出提醒，提醒測試先清掉已有的提醒；T-SHARE-48、T-UPL-32 補進擁有表與案例表；§4.4 的 SQL 表補上 Q7～Q9、Q11～Q14、Q16。測試：`test_trash_lifecycle.py`。 |
| 2026-11-12 | 審查修正（第七輪） | **配額 ledger**：`QuotaLedger` 的量測只在這次執行取得的配額鎖仍被持有時才保留：`_check_many` 在這次執行還沒有接受任何位元組就拒絕時丟掉量測（`base = None`），下一個呼叫取得鎖之後重新量測。測試：`test_quota.py` 的 `TestQuotaRuns.test_a_refused_first_call_does_not_keep_a_measurement_that_no_lock_protects`。 |
| 2026-11-12 | 審查修正（第七輪） | **解鎖拒絕**：`share_unlock` 對已用完來源上限的請求改走 `_source_limited`：不鎖、不重算，只以 `_bump_denied_bucket`（和 `_refuse` 相同的一條語句、自己的短交易）在來源的 `rate_limited` bucket 加一；取不到連線時照樣回 429、該筆不記錄。SC：T-SHARE-49（`dev/sc/sc_unlock_over_limit_together.py`）；測試：`test_audit_log.py`、`test_share_routes.py`。 |
| 2026-11-12 | 審查修正（第七輪） | **PNG 測試**：`test_png_image_data_chunks_follow_the_size_of_the_file`（`test_upload_policy.py`）釘住影像資料 chunk 的額度隨檔案大小成長；`tests/common.py` 新增 `png_chunks_of` 與 `png_with_image_data_pieces`。 |
| 2026-11-12 | 審查修正（第八輪） | **來源計數列**：`_count_below` 的第一條語句沒有符合的列時改為先讀列和上限比較：已到上限才回 0；低於上限的列是第一條語句之後才建立的，就再加一並回傳它的計數；沒有列時照舊 `_minute_is_full`、`INSERT … DO NOTHING`、取槽。並行送出本分鐘第一筆拒絕的請求不再被誤當成已到上限。測試：`test_audit_log.py` 的 `test_a_row_that_another_request_creates_during_the_count_is_counted_on` 等三項。 |
| 2026-11-12 | 審查修正（第八輪） | **稽核保留期 cron**：`_cron_log_retention` 以 `order='id'`（最舊的先）取批，不再對剩下的列計數；批滿（剛好 `LOG_RETENTION_BATCH` 筆）才回報 `remaining` 大於 0，每批的成本不隨資料表大小增加。測試：`test_cron.py` 的 `test_log_retention_takes_the_oldest_rows_first_and_does_not_count_the_rest`。 |
| 2026-11-13 | 審查修正（第九輪） | **ir.default 與 create**：`woow.document` 與 `woow.document.folder` 的 create 只檢查明確帶入的鍵，沒帶入的欄位由 core 以呼叫者的 `ir.default` 補上。現在伺服器設定的欄位沒帶入時一律是欄位自己的 default（`_woow_own_default_fields`，§8.1）；`default_get`、表單預填與明確帶入的值不變。測試：`test_reg_create_ignores_personal_defaults.py`。 |
| 2026-11-13 | 審查修正（第九輪） | **通知的標籤**：`mail.notification` 對 `base.group_user` 開放 read，`mail_message_id` 的標籤是訊息的 `record_name`，受限資料夾與文件的名稱因此會被任何內部使用者讀到。現在 `mail.message._compute_display_name` 對這兩個 model 的訊息以 `sudo(False)._filtered_access('read')` 依真正呼叫者判斷，讀不到的人得到 Restricted folder／Restricted document；`mail.notification` 的 ACL 與規則不動。測試：`test_reg_notification_label.py`。 |
| 2026-11-13 | 審查修正（第九輪） | **成員列的公司**：`woow.document.folder.access.company_id` 是 stored related 欄位，ORM 不拒絕對它的寫入，成員列可因此被移到另一家公司而對該資料夾的管理員不可見。現在 `write` 對非 su 的呼叫者拒絕與資料夾的公司（同時改 `folder_id` 時為目標資料夾）不同的 `company_id`（寫入目前的值不算變更）。測試：`test_reg_access_company_locked.py`。 |
| 2026-11-13 | 審查修正（第九輪） | **文件檔案的索引**：`attachment_indexation` 的 indexer 不看檔案類型，會讀入壓縮檔的成員，工作量不受上傳大小限制。現在 `ir.attachment._index` 在 context 帶 `NO_INDEX_CTX_KEY` 時回傳 False，`WoowDocument.create` 與 `write` 帶這個 context，文件的檔案不產生 `index_content`；全文檢索 bridge（DEC-13）自己決定讀什麼、讀多少。測試：`test_reg_document_file_not_indexed.py`。 |
| 2026-11-13 | 審查修正（第十輪） | **伺服器設定的欄位與預設值**：create 只檢查明確帶入的鍵，沒帶入的欄位由呼叫者存的 `ir.default`（`sudo()` 保留呼叫者的 uid，也保留他存的預設值）或 context 的 `default_*` 補上，伺服器設定的欄位因此可以由預設值決定（稽核列的 `count`、資料夾的 `eff_*`、分享的狀態與密碼雜湊、文件檔案附件的 `public`）。現在每個 model 宣告 `_woow_own_default_fields`，由 `woow.document.server.fields.mixin` 在 create 對這些欄位只取欄位自己的 default，不論呼叫者是不是 su（§8.1）。測試：`test_reg_server_columns_ignore_defaults.py`。 |
| 2026-11-13 | 審查修正（第十輪） | **宣告的檢查**：`woow.document.log` 與兩個 denied model 的清單是全部欄位，`_log_event` 自己寫稽核列的每一欄（`count` 是分享信與下載次數限制的加總依據）；`ir.attachment` 對本模組 binary 欄位的附件用同一個 helper；`tools/tests` 的 `TestServerFieldsDeclared` 在新 model 沒有宣告時失敗，`TestTestFilesAreRegistered` 檢查每個 `test_reg_<slug>.py` 都在 `tests/__init__.py`、`.security/sweep.yaml` 與 `EXPECTED_MIN.json`。 |
| 2026-11-13 | 審查修正（第十輪） | **其他**：補上成員列同時搬到另一個資料夾時以目標資料夾的公司為準（`test_reg_access_company_locked.py`），以及只有資料夾與文件的訊息會折疊標籤（`test_reg_notification_label.py`）的測試；`services/client_ip.py` 以 IPv4 位址比對 IPv4-mapped 的 peer 與 X-Forwarded-For 位址（`test_client_ip.py`）。 |
| 2026-11-13 | 審查修正（第十一輪） | **通知的附件**：core 的 `message_notify` 是公開方法，可指定任何 model 與 id，並以 su 建立或改掛檔案而不檢查該記錄，任何內部使用者因此能把檔案放到本模組的記錄上。現在 `mail.thread._process_attachments_for_post`（`mail_thread_guard.py`）對本模組 model 上、帶附件的 `user_notification` 要求非 su 的呼叫者能 write 該記錄，`message_post` 不檢查（唯讀 follower 照常發文）；CA-36 釘住 core 的行為。測試：`test_reg_notify_attachments_need_write.py`、`test_core_assumptions_access.py`。 |
| 2026-11-13 | 審查修正（第十一輪） | **分享密碼的雜湊**：雜湊內寫了 rounds，passlib 驗證時全部照跑，所以寫入者指定的雜湊會讓公開的解鎖路由做不受限的運算（`tokens.check_password`）。現在 rounds 超過 `PASSWORD_ROUNDS_MAX`（200000）或不是 pbkdf2_sha512 的雜湊不呼叫 verify，`_issue` 對收到的雜湊套同一個檢查（`tokens.is_acceptable_hash`）；由預設值填入 `password_hash` 的部分已由第十輪的 `_woow_own_default_fields` 擋住（精靈與分享都有宣告）。測試：`test_share_token.py`、`test_mail_share.py`、`test_reg_server_columns_ignore_defaults.py`。 |
| 2026-11-13 | 審查修正（第十一輪） | **成員列的鎖**：`woow.document.folder.access` 的 create 與 write 先對請求內每個 folder id 取鎖並更新資料夾列，之後才檢查權限，任何登入帳號都走得到這段。現在非 su 的呼叫者先過 model 權限，folder id 必須是純整數（`'7'` 與 `'07'` 不再是兩個資料夾），且對每個資料夾有 manage 權限，之後才取鎖；su 不變。測試：`test_reg_access_create_authorizes_before_locking.py`。 |
| 2026-11-13 | 審查修正（第十一輪） | **portal 的 description**：portal 聯絡人可在 `woow.document.create` 帶 `description`（HTML），core 對它 sanitize 兩次，對某些標記成本隨長度平方成長。現在非內部使用者的 description 超過 `PORTAL_DESCRIPTION_MAX_CHARS`（2000）字元 → `WoowUploadRejected('content')`，在配額鎖與寫入之前拒絕。測試：`test_reg_portal_description_cap.py`。 |
| 2026-11-13 | 審查修正（第十一輪） | **JPEG 的 scan**：`_jpeg_structure_ok` 在第一個 SOS 就放行，之後的 scan 個數沒有上限，而 Odoo 儲存與縮圖時會解碼整張圖。現在走到 EOI 為止，scan 最多 `JPEG_MAX_SCANS`（64），scan 的資料以一次搜尋跳到下一個標記，EOI 之後的位元組不看（§7.1）。測試：`test_upload_policy.py`。 |
| 2026-11-13 | 審查修正（第十一輪） | **回收桶保留天數 0**：core 把設定表單的整數 0 存成沒有值（刪掉參數），讀回是預設的 30 天，每日 purge 因此照常刪除。現在 `res.config.settings.set_values` 在 core 之後把 0 以字串 `'0'` 寫回。測試：`test_settings_bounds.py`。 |
| 2026-11-13 | 審查修正（第十二輪） | **替換分享連結**：`models/share_service.py:361`（修正前的行號）的 `action_reissue` 讓新連結以按下按鈕的人為建立者，範圍規格由他的環境讀出，之後每個公開請求都以新建立者的環境重算範圍。文件管理員替換別人的連結時，新連結因此多涵蓋原建立者看不到的受限子資料夾與已失去權限的文件，和「同一個範圍」的約定不符；持有新連結的人可列出並下載原連結範圍之外的文件。現在只有建立者本人能替換：`create_uid` 不是呼叫者 → `AccessError`，舊連結不動；管理員要結束別人的連結用 `action_revoke`（不變），要發新連結走分享精靈，由他自己明確選範圍。測試：`test_share_routes.py` 的 `TestShareReplacementIsForTheCreator`。 |
| 2026-11-13 | 審查修正（第十二輪） | **並行測試**：補上三組釘住既有行為的測試，沒有改任何非測試檔案：配額的站台鎖 `lock('quota')`（`test_quota.py`）、`_bump_denied_bucket` 與 `_count_below` 取不到容量槽時的 `rollback`（`test_audit_log.py`）、`share_unlock` 比對密碼前的 `_lock_password_attempts` 與計入失敗時寫 share 列（`test_share_routes.py`）；每一項都以刪除被守護的程式行驗證測試會失敗。 |
| 2026-11-13 | 移除連結式分享 | **移除連結式分享**（負責人決定：不使用）。本模組沒有以連結取得文件的功能：移除 model `woow.document.share`、`woow.document.share.wizard`、`woow.document.denied.budget`、`woow.document.denied.counter`，七條 `/woow-docs/s` 公開路由（`auth='public'` 的路由不再有）、`services/tokens.py`（token 與密碼雜湊）、分享信範本、分享維護 cron、分享用的設定鍵（8 個，另 `share_link_base_url` 改名為 `notice_base_url`，只給成員通知信用）、9 種稽核事件與 `share` channel、`denied` 結果（稽核欄 `result` 只剩 `ok`，`share_id`、`link_ref`、`bucket_minute`、`count` 欄位移除）、`effective_restored_at` 與 `subtree_restored_at`（只給分享範圍用）、CA-15 與 SC 的分享執行器，以及對應的測試、靜態檢查的規則與清單、翻譯。保留：資料夾權限與直接授權（`shared_user_ids`、`shared_partner_ids` 是授權，不是連結）、portal 頁面與已登入使用者的下載和 ZIP、上傳政策與配額、回收桶、搬移與通知精靈、稽核紀錄（12 種事件；客戶端 IP 的雜湊與顯示照舊，`trusted_proxies`、`client_ip_header`、`log_ip_mode` 保留，雜湊金鑰改由 `settings._audit_ip_key()` 取得，label 不變）。數字：model 19→15、路由 16→9（entry point 17→10）、設定鍵 30→22、事件 21→12、ACL 26→22、rule 33→29、cron 4→3、郵件範本 2→1。模組沒有在任何正式站安裝過，不寫資料搬移、不留相容層，版本維持 18.0.1.0.0。以上各節與先前變更紀錄中提到分享的部分，以本列為準。 |
| 2026-11-13 | 移除分享後的測試與用語 | **還原時間與稽核讀取規則的測試、移除功能的用語**：`restored_date` 在每一種還原方式之後（文件、資料夾，以及兩者經還原精靈）都檢查，先檢查它原本是空的（`test_trash_lifecycle.py`）；新增一項測試釘住誰讀得到哪些稽核列與欄位：文件使用者只讀自己的列、管理員讀所屬各公司的全部列、portal 與只屬於 `base.group_user`（兩個文件群組都不在）的一般內部使用者全部讀不到、位址欄位只給管理員、ORM 不能建立、修改或刪除（`test_audit_log.py`）。鎖表的 ZIP 列、`zip_response` 的說明、`sql-ok` 標記的清單、SECURITY.md 的 proxy 說明、`SUDO-ENV-REUSE` 的訊息與 `_woow_filter_rank` 的 docstring 改成與程式一致。只改測試與文字，行為不變。 |
| 2026-11-13 | 審查修正（移除分享後） | **附件不帶也不收 token**：core 以 su 為 chatter 附件產生 access token（portal 的 `/mail/chatter_fetch`、超限信件的附件連結），`ir.attachment.write` 的守衛對 su 不生效，token 存入後不需登入就能下載，撤權與進回收桶之後仍有效。現在 `generate_access_token()` 對 `WOOW_MODELS` 的附件不寫 token（回傳清單該位置為 `False`），`validate_access` 與 `ir.binary._find_record` 對這些附件拒絕任何 token（含已存入的值與簽章 token，結果同錯誤 token）。超限的 chatter 附件仍從信裡拿掉、換成下載連結，連結沒有 token，只有已登入、能讀該記錄的內部使用者打得開，其他人含 portal 使用者得 404（設計決定）。不新增 sudo 與 raw SQL；`tools/public_methods.txt` 增列兩個覆寫，`check_forbidden` 只放行覆寫內的 `super()`（§4.6、§8.6、§10、SECURITY.md HD-04／HD-12）。測試：`test_reg_chatter_attachment_tokens.py`。**設定鍵 `mail_subject_include_name` 移除**：它只管已移除的分享信主旨，沒有讀取點；從設定讀取器、表單、兩份翻譯目錄與 §6.1 移除，鍵數 22→21。測試：`test_settings_bounds.py`，以及 `test_rpc_exposure.py` 的設定欄位下限（`woow_doc_*` 欄位至少 21 個，原為 22）。模組沒有在任何正式站安裝過，兩項都不寫資料搬移。 |
| 2026-11-13 | 審查修正（第二輪，移除分享後） | **附件的 public 旗標與 token 在所有 env 都不留在本模組記錄的附件上**：前一列讓 `generate_access_token` 不再寫 token、取用端拒絕任何 token，但核心還有兩處以 su 寫入，create 與 write 的守衛對 su 不生效：`mail.thread._process_attachments_for_post` 以 su 的 write 把發文者的待送檔案（`mail.compose.message`、`res_id` 為 0，發文者可以對自己建立的這一列設 `public=True` 與 `access_token`）改掛到記錄上，旗標與 token 跟著上去，沒有 session 的訪客就得 200（帶 `unique=1` 時回應還能被共用快取保存一年），撤權與進回收桶之後仍然打得開；`_create_attachments_for_post` 以 su 建立內文引用的檔案，帶著 core 剛產生的 token，create 的守衛只看非 su，token 因此存進列裡（取用端雖然拒絕它，文件說的「本模組記錄的列不帶 token」並不成立）。能觸發的有編輯者，以及追蹤記錄的唯讀成員（follower 可以發文）。現在 `create` 與 `write` 在所有 env 都讓本模組 model 的列沒有 `public` 與 `access_token`：非 su 照舊 AccessError；su 不丟例外，create 以 False 取代（含 context 與 `ir.default` 的預設值），write 在 `res_model` 改成本模組 model 時對每一列一併寫入 False，只設定 `public` 或 `access_token` 時依各列目前的 `res_model` 分開寫入（混合的一組只改該改的列），其他寫入不讀任何列、不增加查詢。不新增 sudo、`with_user`、`env(su=True)` 與 raw SQL；`generate_access_token`、`validate_access` 與 `ir.binary` 的規則照舊（縱深防禦）。**超限信件**：連結沒有 token，只有已登入、能讀該記錄的內部使用者打得開，沒有 session 的人與 portal 使用者都得 404（§8.6、§10、SECURITY.md HD-04）；文件中先前與此不符的說法已更正。**文件**：R-SU 表、§8.6 的 su 描述與 `generate_access_token` 的呼叫者清單改成與程式一致。測試：`test_reg_module_attachments_never_public.py`；`test_reg_chatter_attachment_tokens.py` 的「已存入的 token」改以繞過 ORM 的敘述寫入（ORM 不再存它），並新增 portal 成員的信件與連結。**測試補強**：`validate_access` 的測試對 `WOOW_MODELS` 的每個 model 各建一列（把覆寫縮到單一 model 會失敗）；內嵌圖片的測試也檢查存入的內文網址是 `access_token=False`；測試使用者改用 `new_test_user` 並帶 `no_reset_password`（不排入註冊信）；`tools/tests` 新增 `F-TOKEN-API` 的第四個反例（覆寫內呼叫的是別的 token 函式，釘住比對呼叫名稱與覆寫名稱的那一個條件，§4.6）；設定表單的 `woow_doc_*` 欄位與 `_PARAM_KEYS` 一對一的測試（`test_settings_bounds.py`）。 |
| 2026-11-13 | 審查修正（第三輪，移除分享後） | **token 與欄位名稱依欄位實際存下的內容判斷**：Char 欄位對每個不是 `None` 或 `False` 的值存 `str(value)`，`access_token` 或 `res_field` 為 `0`、`0.0`、`[]`、`{}`、`()` 時存成 `'0'`、`'0.0'`、`'[]'`、`'{}'`、`'()'`，守衛卻以 Python 的真值判斷，放行了它們：有文件編輯權的使用者以 ORM 或 JSON-RPC `call_kw` 建立 `access_token=0` 的列、把自己的待送檔案連同 `access_token=0` 搬上文件，su 的 create 與 write 也照存（`public` 是 Boolean，ORM 存 `bool(value)`，不受影響）。存下的值不能打開檔案（取用端拒絕任何 token），但破壞「本模組記錄的列不帶 token」的不變式，而 core 的 `_check_attachments_access` 接受存著的 token：沒有授權的使用者可以用它把受限檔案連到自己的訊息、讀到檔案的中繼資料。現在 `models/ir_attachment.py` 只有一個定義 `char_is_set(value)`（不是 `None`、不是 `False`、`str(value)` 不是空字串），create 的非 su 檢查（`res_field`、`access_token`）、write 的非 su 檢查（`res_model` 與 `res_field` 一起寫入、搬移時寫入的 `res_field` 與 `access_token`）、`_woow_check_field_target`（所有 env）與欄位檔案的 own default 都用它；`public` 照真值；`False`、`None` 與空字串照舊放行。su 的重設不再看值：create 的寫入值或預設值、write 的鍵，只要帶了 `public` 或 `access_token`（不論值），本模組 model 的列兩欄都設成 False；`_woow_write_closed` 的說明改成與程式一致（旗標與 token 一併寫入，不是只寫為真的那個鍵）；沒有帶這兩個鍵、也沒有帶本模組 model 的 write 仍然不讀任何列。不新增 sudo、`with_user`、`env(su=True)`、raw SQL 或資料庫限制式。**文件**：§8.6 內文引用的小節名稱改成現有的名稱；發行端一條不再說「兩條路」，改為指向呼叫者清單；呼叫者清單補上 `html_editor` 與 `web_editor` 的媒體對話框（以使用者自己的環境、經 RPC 呼叫，覆寫涵蓋）；寫入端寫明 `mail.compose.message` 與 `mail.thread._message_update_content` 也走到 `_process_attachments_for_post`；最後一條寫明只對非 su 拒絕、su 改由寫入端重設；成本的說法改成「一條 SELECT」；測試一條補上 portal 成員的兩項測試；SECURITY.md HD-12 補上 public 旗標，與 HD-04 合為同一條規則；`TestLargeFilesInMail` 的說明只對其他 model 說連結帶 token。**測試**：`test_reg_module_attachments_never_public.py` 新增：每個本模組 model 與 `0`、`0.0`、`[]`、`{}`、`()` 的 su create 與 write（含旗標同時寫入、搬移、混合的一組、已存入的值）、非 su 的 create 與搬移（token 與 `res_field`，ORM 與 JSON-RPC）、su 搬移為欄位附件被拒絕、空值照舊放行、其他 model 的對照、欄位檔案的 own default；預設值的三種來源（呼叫者與站台的 `ir.default`、context）對貼文、內嵌圖片、通知與待送檔案的 su create，以及對非 su 的 create；`message_notify(attachment_ids=…)` 搬移待送檔案；對照測試對其他 model 的公開檔案做與關閉測試相同的每一種請求；write 的成本以 `cr.sql_log_count` 釘住（冷快取多一條 SELECT，與列數無關）；「哪些 write 會被看一眼」的測試改成依鍵的有無，不依值。每一項都以還原對應的修正驗證測試會失敗。 |
| 2026-11-13 | 審查修正（第四輪，移除分享後） | **資料夾的 owner 受授權上限與文件使用者的限制**：owner 在 `_compute_effective` 得到 manage（rank 4），但授權上限（不繼承的資料夾只能收窄上層的授權，AC-2）只實作在成員列：`write` 的 `owner_id` 只要求呼叫者對資料夾有 manage，不看新 owner 在上層的等級；`create` 對上層有 manage 的人放行明確帶入的 `owner_id` 與 `inherit_access=False`。對資料夾有 manage 的成員因此能把不繼承的資料夾交給在上層沒有任何授權的使用者，對方取得該資料夾與繼承它的子樹的 manage（讀、改、丟回收桶，含日後新增的文件，並可移除或調降成員），同一個動作走成員列卻被拒絕（`A member cannot be granted more access than on the parent folder.`）；`owner_id` 也不要求是文件使用者（arch §3.1），owner 變更也不留稽核列。設計決定（負責人）：AC-2 涵蓋 owner 的隱含 manage，owner 要是內部的文件使用者。現在 `create` 與 `write`（非 su）：新 owner 要是文件使用者（`Choose a document user.`，與成員列同一個訊息，翻譯目錄不增加）；資料夾（寫入或建立之後）不繼承、有上層，而呼叫者不是文件管理員時，新 owner 還要在上層有 manage 權（對象是 owner 的 `_woow_has_rank(4)`：列在上層、經群組，或是文件管理員），否則 `A member cannot be granted more access than on the parent folder.`；寫入現有的 owner 不算變更。owner 不變、資料夾搬到別的上層或停止繼承時，現有的 owner 也要過授權上限，所以不能分兩步繞過（先在繼承時指定 owner 再關閉繼承，或搬到 owner 沒有權限的上層）。create 的 owner 包含沒帶入時由呼叫者存的 `ir.default` 補上的值；編輯者（對上層只有 edit）仍不能選 owner。`inherit_access` 與 `owner_id` 的值依 ORM 存下的內容判斷（`0`、`None` 都是關閉繼承；0、負數、字串、浮點數、布林不是 owner）。呼叫者讀不到上層時，上層的授權以呼叫者的身分搜尋，找不到，答案同「owner 在上層沒有權限」，不論 owner 是誰，不洩漏看不到的上層有哪些成員；不新增 sudo、raw SQL 與白名單項目。owner 改變（含清空）寫一列 `access_change`（`detail` 是 `{"owner_id": 新, "previous_owner_id": 舊}`，沒有 owner 記 0）。**行為改變**：不是文件管理員的 manager 不能再把不繼承的資料夾交給在上層沒有 manage 權的人，也不能在 owner 沒有權限的上層底下搬動它或讓它停止繼承，要先在上層給 manage 權，或由文件管理員處理；讀不到上層的 manager 不能把這種資料夾交給別人。文件管理員與 su 不受授權上限，owner 對非 su 仍要是文件使用者。沒有全域的 `@api.constrains('owner_id')`（su 建立資料夾時 owner 預設是 su 自己，常不是文件使用者）。測試：`test_reg_owner_within_ceiling.py`。 |
| 2026-11-13 | 審查修正（第五輪，移除分享後） | **旗標依 ORM 存下的值判斷，不看帶入的值是什麼型別**：Boolean 欄位存 `bool(值)`，`0`、`0.0`、`None`、空字串與空 list 和 `False` 一樣存成假（core 的 `fields.Boolean.convert_to_column` 與 `convert_to_cache`），但 `woow.document.folder` 的 `create` 與 `write` 對 `inherit_access` 用 `vals.get('inherit_access') is False` 判斷，只有物件 `False` 為真（修正前 `models/document_folder.py` 第 205 行：create 的編輯者檢查；第 252、254 行：「先存成員變更」與「先存新的上層」；第 282 行：成員列的授權上限與把繼承來的權限轉成成員列）。以 ORM 或 JSON-RPC 對繼承中的資料夾 write `inherit_access=0`（或 `0.0`、`None`、空字串、空 list）時，資料夾照樣變成不繼承，卻沒有過成員列的授權上限，也沒有把繼承來的權限轉成成員列：對資料夾有 manage 的成員可以讓不繼承的資料夾留著在上層沒有授權的成員列（「不繼承的資料夾沒有超過上層的成員」被破壞，同一個授權帶 `False` 會被拒絕），原本靠繼承取得權限的成員、群組與 portal 聯絡人在資料夾變成不繼承之後失去權限，同一次寫入也能帶 `access_ids` 或 `parent_id`；對上層只有 edit 的編輯者以同樣的值 create 子資料夾，繞過「編輯者不能關閉繼承」的檢查，得到不繼承的資料夾。現在 `models/document_folder.py` 只有一種讀法 `_stored_flag(vals, name, default)`：有帶這個鍵就是 `bool(值)`，沒帶就是 `default`；create 的編輯者檢查與 owner 檢查、write 的兩個「先存」檢查、授權上限與轉成成員列、`_woow_check_owners` 都用它；`True`、`1`、非空字串（含 `'false'`）照真，`False` 與沒帶這個鍵照舊。**全模組檢查**（models、wizards、controllers、services）：沒有其他地方以 `is False`、`is True`、`is not False`、`is not True`、`== False`、`== True`、`!= False`、`!= True` 判斷呼叫者帶入的 Boolean 欄位值；`legal_hold`（`action_set_legal_hold` 的 `bool(hold)`）、`active` 與 trash 的欄位（以有沒有帶這個鍵判斷，不看值）、`public`（照真值）原本就依存下的值；`char_is_set`、`_company_value`、`_woow_owner_value` 的 `is not False` 是 Char 與 many2one 欄位的空值，不是 Boolean。不新增 sudo、`with_user`、`env(su=True)`、raw SQL 與白名單項目，沒有改任何其他守衛的語意。**測試**：`test_reg_typed_boolean_flags.py` 新增 8 項（write：成員列超過上層、轉成成員列（呼叫者是 manager、文件管理員與 su）、先存成員變更、先存新的上層、owner；編輯者的 create 與 write；manager 的 create（owner 與成員列）；讀法本身與 ORM 存下的值），值各取 `False`、`0`、`None`、空字串、空 list、`0.0`（都與 `False` 一樣）與 `True`、`1`、`2.5`、文字、`'false'`、`[0]`（都與 `True` 一樣）；重現的案例（成員列的使用者在上層沒有授權，write `inherit_access=0`）在修正前失敗、修正後通過；每一項都以還原對應的修正驗證測試會失敗。 |
