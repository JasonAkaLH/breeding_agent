# 全仓业务代码渐进式架构清理 P6 实施计划

## 1. 状态与硬边界

- 日期：2026-08-25
- 分支：`main`
- 状态：`complete`
- P6 start commit：`e49a0398dde2a939266e4edf352c8200750278f9`
- P6 start tree：`70bd7f96808264c447146166337e52911b8d1003`
- P6 start tracked set：1072

P6只处理`frontend/src/**`内三类已证明结构问题：App中两份exact Set删除callback、Attachment pure types/helpers与展示组件混在App、Interrupt pure types/helpers与展示组件混在App。App仍唯一拥有auth/conversation/messages/composer、首次normal/Slash/MCP submit、upload/delete/rollback effect、Interrupt answer协调、Task Runtime/SSE/timer/cancel/artifact effect；P6不改API/wire、state transition、DOM/a11y、文案、视觉、CSS、focus/scroll、storage、后端或P0 deferred behavior。

## 2. ai-slop-cleaner finding register

| Finding | 分类 | 证据 | P6处置 |
|---|---|---|---|
| `P6-SET-DELETE-001` | exact duplication | conversation delete/rename finally各复制同一3-statement Set clone/delete callback；upload删除为同一语义 | 建立单一pure generic helper并复用，不改变React setter时点 |
| `P6-ATTACHMENT-DOMAIN-001` | structural candidate | App底部包含Attachment types、8个pure formatter/merge helper和2个presentational card | types/helpers移入domain，cards移入components；App-local effect controller不动 |
| `P6-INTERRUPT-DOMAIN-001` | structural candidate | App底部包含Interrupt/Sheet/Slot types、14个pure metadata/parser/helper和2个presentational component | 原样迁入domain/components；answer API与state/effect owner不动 |
| `P6-APP-MONOLITH-001` | reviewed_no_change | `App.tsx`约3,814行，持有大量跨scope closures与React refs | 只迁移无effect的pure/presentational块；不以行数强拆controller |
| `P6-TASK-RUNTIME-001` | reviewed_no_change | subscribe/event/reconnect/waiting/cancel/artifact约430行共享generation/ref/patch buffer | 保持App唯一owner；无新增hook、subscription、timer或message store |
| `P6-ATTACHMENT-CONTROLLER-001` | reviewed_no_change | upload/delete/reload/rollback/commit约260行与App optimistic/history/notice协调 | 保持App-local唯一controller；不建立第二套state/API owner |
| `P6-SLASH-MCP-PARSER-001` | reviewed_no_change | Slash与MCP command各16个function、形状相似 | `/`与`$` token、Unicode folding、case、conflict、metadata/routing合同不同，不抽generic parser |
| `P6-DEFERRED-UI-001` | deferred behavior | MCP菜单方向键、Artifact ID推断、localStorage/API text fallback、upload refresh及UI调整 | 不修复、不混入P6 |

Frontend production TypeScript三语句以上exact duplicate为1组，即`P6-SET-DELETE-001`；其它相似代码均按完整语义审查。

## 3. Checkpoints

### Checkpoint A：计划、范围与基线

运行：

- Frontend全量21 files / 307 tests；
- `npm run typecheck`；
- `npm run build`。

三项均已通过；build只有既有`vendor-antd`大于500 kB warning，P6不做code splitting。冻结：

- App是messages store与`pendingAssistantPatches`唯一owner；
- normal/Slash/MCP首次submit=`App 1 / TaskRuntime 0`；
- Interrupt answer=`App 0 / 当前answer coordinator 1`，pre-upload失败=0；
- upload串行、uploading、rollback、history reload、optimistic turn、finally顺序；
- subscription close/open、generation/conversation/task/assistant guards、waiting→interrupt→close、terminal artifact→clear；
- DOM wrapper/className/role/name/ARIA/focus/scroll/portal/welcome mount与CSS；
- 21个测试文件集合与307项基线。

完成：`a5dee57`（`docs(cleanup): plan P6 frontend boundaries`）。

### Checkpoint B：复用pure Set删除helper

新增最小`domain/collections.ts`，返回clone后删除指定值的新Set。conversation delete、rename和upload deletion setter复用；setter调用位置、finally、key和返回identity语义不变。直接测试覆盖命中/未命中、原Set不变和新Set identity。

完成：`a8833fe`（`refactor(frontend): reuse immutable Set removal`）；聚焦2 files / 128 tests与typecheck通过。

### Checkpoint C：分离Attachment pure domain与cards

新增：

- `domain/attachments.ts`：`DraftAttachmentStatus`、`DraftAttachment`、`UploadedDraftAttachment`及8个pure helper；
- `components/AttachmentCards.tsx`：`DraftAttachmentCard`、`ConversationFileCard`。

方法体/JSX AST逐项保持；App通过import使用同一type/helper/component，不复制定义。不得移动`draftAttachments/pendingUploads/uploading/deleting` state，不移动upload/delete/reload/rollback/commit API effect，不改变Drawer/input refs、DOM、aria-label或notice。新增domain直接测试，App existing tests负责DOM/effect trace。

完成：`8854310`（`refactor(frontend): separate attachment presentation`）；迁移声明13/13 AST-text等价，聚焦2 files / 131 tests与typecheck通过。

### Checkpoint D：分离Interrupt pure domain与presentation

新增：

- `domain/interrupts.ts`：`PendingInterrupt`、Sheet/Slot types及14个pure metadata/parser/presentation-value helper；
- `components/InterruptPresentation.tsx`：question text与composer status两个presentational component。

函数/JSX AST逐项保持。`handleInterruptAnswer`、optimistic turn、`api.submitMessage`、keep-open/resumed/rejected/stale、Attachment disposition、pending interrupt、subscription和Task state全部留在App原调用位置。新增direct domain fixtures覆盖metadata/upload/sheet/keep-open/natural-language/slot-ref/reserved fields；App full测试锁行为与DOM。

完成：`b438f62`（`refactor(frontend): isolate interrupt domain and presentation`）；迁移声明20/20 AST-text等价，聚焦2 files / 133 tests与typecheck通过。

### Checkpoint E：全量门禁与终态handoff

复跑Frontend full、typecheck、build，要求新增测试零skip且原307项继续全绿。用TypeScript AST确认：

- App无迁移type/helper/component第二定义；
- production exact duplicate组从1降为0；
- `api.submitMessage`、event source、timer、upload/delete API及messages setter owner/call count相对start不增加；
- Task Runtime/Attachment effect仍只有App一个owner。

Backend、Rust、真实PostgreSQL、Linux Parser与外部MCP因wire/生产路径未触及记为N/A。同步本计划、`docs/AGENTS.md`、`frontend/AGENTS.md`与`CHANGELOG.md`，冻结P7 handoff。

终态结果：

- Frontend全量24 files / 321 tests零失败、零skip；typecheck与production build通过；只有既有`vendor-antd`大于500 kB warning；
- production TypeScript三语句以上exact block duplicate从1组降为0组；
- `api.submitMessage` 3→3、upload API 1→1、delete upload API 2→2、`setMessages` 13→13、`setPendingUploads` 9→9、timer 4→4、subscription ref 26→26，均未增加；
- `App.tsx`从约3,814行降至3,388行，只迁出已登记pure/presentational声明；
- P6 implementation HEAD=`b438f62c7fe30a3c98f8efbf4fdde510a990055b`，tree=`077a41b289ad2f0fc5b99c6514aae2e85dd1a9c9`；终态tracked set=1081，路径清单SHA-256=`fc319a6f9fd6c2802c99957399b977a6c756dbe155c8f87d682503e69464363b`；
- `docker_cmd.md`仍存在、被ignore且未跟踪；Backend、Rust、真实PostgreSQL、Linux Parser与外部MCP为未触及N/A。

提交：`docs(cleanup): close P6 frontend boundaries`

## 4. 必须保持的合同

- API request/response/type/event wire零变化；不新增/删除/重排API调用；
- App仍唯一拥有auth、conversation restore generation、messages、composer、commands与首次submit；
- Attachment effect controller仍在App且唯一，串行upload/rollback/reload/commit/delete trace不变；
- Task Runtime仍在App且唯一，subscription/timer/setup/cleanup与scope guard不变；
- Interrupt answer outcome矩阵的submit/subscription delta、optimistic turn、Attachment disposition和pending interrupt不变；
- pure domain无React、ApiClient、EventSource、timer、DOM或storage依赖；
- presentational components无API/subscription/state authority；
- DOM/ARIA/class/style/focus/scroll/portal/StrictMode行为不变；
- dependencies、public assets、backend、schema/data、`prod`与`docker_cmd.md`正文不变。

## 5. 停止与回滚

若迁移函数/JSX不能保持AST、若需要移动effect才能编译、若App tests显示DOM/API/timer/subscription delta、若typecheck/build变化、若需要修改后端或CSS，则停止该候选并保留已绿检查点。

每个检查点独立commit，逆序revert即可。App/Task Runtime/Attachment controller的进一步hook化、Slash/MCP generic parser和所有deferred UI问题均不在P6；P8也不得把行为敏感closure误删为dead code。
