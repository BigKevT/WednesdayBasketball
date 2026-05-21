# Wednesday Basketball

固定球友每週三籃球報名網站 MVP。

## 功能

- 首頁顯示下一場球局摘要。
- 詳情頁可查看正式名單、候補名單並報名。
- 正式名單 15 人，候補名單 5 人。
- 每週三中午 12:00 後，球友端停止報名。
- 每個名字旁可直接取消。
- 正式名單取消後，候補第一位自動遞補。
- 管理後台可新增、取消、調整正式與候補名單。
- 管理後台可設定固定報名者，每週新場次會自動報名。
- 本機使用 SQLite，部署時使用 Supabase Postgres 保存歷史紀錄。

## 本機啟動

安裝套件：

```bash
python3 -m pip install -r requirements.txt
```

啟動：

```bash
python3 app.py
```

打開：

- 球友端：http://127.0.0.1:8789
- 管理後台：http://127.0.0.1:8789/admin

預設管理密碼：

```text
admin
```

可用環境變數修改：

```bash
ADMIN_PASSWORD=your-password python3 app.py
```

若沒有設定 `DATABASE_URL`，本機會使用 `basketball.db` SQLite 檔案。

## 免費部署

建議使用：

- Vercel Hobby：部署網站。
- Supabase Free：提供 Postgres database。

### 1. 建立 Supabase 專案

1. 到 Supabase 建立 Free project。
2. 進入 Project Settings。
3. 找到 Database connection string。
4. 複製 Postgres connection string。
5. 若 connection string 沒有 `sslmode=require`，請在最後加上。

格式通常類似：

```text
postgresql://postgres.xxxxxx:YOUR_PASSWORD@aws-0-region.pooler.supabase.com:6543/postgres?sslmode=require
```

### 2. 上傳到 GitHub

```bash
git init
git add .
git commit -m "Initial Wednesday Basketball app"
git branch -M main
git remote add origin YOUR_GITHUB_REPO_URL
git push -u origin main
```

### 3. 在 Vercel 匯入 GitHub Repo

1. 到 Vercel 新增 Project。
2. 選擇這個 GitHub repo。
3. Framework Preset 可維持 Other。
4. 加入 Environment Variables：

```text
DATABASE_URL=你的 Supabase Postgres connection string
ADMIN_PASSWORD=你的管理密碼
```

5. Deploy。

部署後，Vercel 會依照 `requirements.txt` 安裝 Flask 與 psycopg，並用 `vercel.json` 將所有路由導向 `app.py`。

## 檔案

- `app.py`：Flask 網站、路由、資料庫與名單規則。
- `requirements.txt`：Vercel / 本機需要安裝的 Python 套件。
- `vercel.json`：Vercel 路由設定。
- `basketball.db`：啟動後自動建立的 SQLite 資料庫。
- `wednesdayBasketball.md`：PRD。
