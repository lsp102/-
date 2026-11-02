# 发布到 GitHub

> 机器上已有 **GitHub Desktop 3.5.8**（自带 git），不需要另装 Git。
> 本目录已经是一个初始化好的 git 仓库，并有一次提交 —— 你只需要把它推上去。

---

## ⚠️ 先决定：公开还是私有

**建议先建私有仓库。** 理由：

1. 未来杯章程里如果有"作品须为原创、未公开发表"这类条款，公开仓库可能被认定为已发表
2. 决赛在 12 月，期间不希望对手看到你的架构和实验数据
3. 私有仓库同样可以加队友为 collaborator，共享完全不受影响

**比赛结束后再转公开**（Settings → Danger Zone → Change visibility），不影响开源展示。

如果确认章程允许公开，那就公开，反而能给项目加分。

---

## 方式 A：GitHub Desktop（推荐，5 步）

1. 打开 GitHub Desktop，登录你的 GitHub 账号
2. 菜单 **File → Add local repository…**
3. 选择本目录：`C:\Users\李圣鹏\Desktop\FallGuard`
4. 右上角点 **Publish repository**
   - Name: `fallguard-harness`
   - **取消勾选** "Keep this code private" 前的反向操作 —— 即**勾上** Keep private（如果要私有）
5. 点 Publish

完成后仓库地址形如 `https://github.com/<你的用户名>/fallguard-harness`

---

## 方式 B：命令行

需要先装 Git（`winget install --id Git.Git -e`），或用 GitHub Desktop 自带的：

```powershell
$git = "$env:LOCALAPPDATA\GitHubDesktop\app-3.5.8\resources\app\git\cmd\git.exe"
```

先在 GitHub 网站上手动建一个**空仓库**（不要勾 README / .gitignore），然后：

```powershell
cd C:\Users\李圣鹏\Desktop\FallGuard
git remote add origin https://github.com/<你的用户名>/fallguard-harness.git
git branch -M main
git push -u origin main
```

首次 push 会要求登录。推荐用 Personal Access Token 当密码
（GitHub → Settings → Developer settings → Personal access tokens → Fine-grained，勾 `repo`）。

---

## 提交身份要先改

仓库里的那次初始提交用了一个**占位身份**。发布前改成你自己的，否则 GitHub 上不会关联到你的头像：

```powershell
cd C:\Users\李圣鹏\Desktop\FallGuard
git config user.name  "你的名字"
git config user.email "你的GitHub邮箱"
git commit --amend --reset-author --no-edit
```

之后每次提交都会用这个身份。

---

## 加队友

仓库页面 → **Settings → Collaborators → Add people**，输入队友的 GitHub 用户名。
他们接受邀请后就能 clone、提交、开 PR。

队友首次拉取：

```powershell
git clone https://github.com/<你的用户名>/fallguard-harness.git
cd fallguard-harness
python -m harness.cli validate fall-response   # 确认环境正常
```

---

## 日常提交

```powershell
git add -A
git commit -m "feat(engine): 支持 foreach 嵌套"
git push
```

建议约定：`feat:` 新功能 / `fix:` 修 bug / `docs:` 文档 / `exp:` 实验数据。
初赛材料里如果放仓库链接，有清晰的提交历史是加分项。

---

## 提交前检查清单

- [ ] `.gitignore` 已生效，没有 `__pycache__`、`results.csv`
- [ ] 没有 `.env`、密钥、token（本项目零密钥，但确认一下）
- [ ] 提交身份已改成本人
- [ ] 仓库可见性符合赛制要求
- [ ] README 里的命令能跑通（`python -m harness.cli validate fall-response`）

---

## 已经准备好的内容

```
LICENSE         MIT
.gitignore      Python + 运行产物 + 编辑器
Dockerfile      零依赖镜像，可直接部署
DEPLOY.md       云端部署方案对比
README.md       项目说明
PLATFORM.md     平台规范（节点原语 / pack 格式 / 断言语法）
```
