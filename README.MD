# Reverse Draw

A web app for running a reverse draw. It starts with 1000 tickets, and each round cuts the draw down: 500, 250, 100, 50, then 1 winner.

- **Public page (`/`)** opens on a landing screen that asks for the 6-digit access code. After that it shows the announcement band, the headline counts, the ticket board, an "Am I still in?" lookup, the round list and a short "How it works". It picks up new results every 10 seconds.
- **Admin page (`/admin`)** needs a password. From it you run, undo and reset rounds, manage ticket holders, view the elimination log and download CSV files.

The stack is FastAPI (Python), plain HTML/CSS/JS and Postgres (Supabase). Everything runs as one Render web service.

## Project layout

```
app/
  config.py      <- all adjustable settings (rounds, text, colours, privacy)
  draw.py        draw engine (pure logic)
  db.py          storage: Postgres if DATABASE_URL is set, otherwise a local SQLite file
  main.py        web server + API
  static/        frontend (index.html, admin.html, styles.css, *.js)
render.yaml      Render blueprint
requirements.txt
```

## Run it on your computer

```
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # Windows: copy .env.example .env   -> then edit ADMIN_PASSWORD
uvicorn app.main:app --reload
```

Then open http://localhost:8000 for the public page and http://localhost:8000/admin for the admin page.

When `DATABASE_URL` is empty, the app saves everything to a local `reverse_draw.db` file. That file is only for testing on your own machine.

## Public access code
The code people type to see the board is `PUBLIC_ACCESS_CODE` in `app/config.py` (default `123456`, so change it). If your GitHub repo is public, anyone can read that file. In that case, add `PUBLIC_ACCESS_CODE` as an environment variable in Render instead; it overrides the value in `config.py`. Changing the code signs everyone out, and admins never need it. Set it to `""` to turn the code off.

## Change the settings

Edit `app/config.py`, then commit and push. Render redeploys automatically. Once Round 1 has run, the round schedule is locked for that draw, and new round settings only apply after an admin resets the draw.

## Deploy (GitHub + Supabase + Render)

### 1. Database (Supabase)
1. Create a project at supabase.com and save the database password it gives you.
2. Click **Connect** at the top of the project, then copy the **Session pooler** connection string. Don't use "Direct connection": that one is IPv6-only, and Render can't reach it.
3. In the string, replace `[YOUR-PASSWORD]` with your database password.

You don't need to create any tables. The app creates its one table (`draw_state`) on first start.

### 2. GitHub
```
git remote add origin https://github.com/<you>/reverse-draw.git
git push -u origin main
```
(Create the empty `reverse-draw` repo on GitHub first. Don't add a README there.)

### 3. Render
1. In Render, go to **New → Blueprint** and choose your repo. Render reads `render.yaml`.
2. When it asks for environment variables:
   - `DATABASE_URL`: the Supabase Session pooler string from step 1
   - `ADMIN_PASSWORD`: the password for `/admin`
   - `SECRET_KEY` is generated for you automatically.
3. Deploy. Your site will be at `https://reverse-draw-XXXX.onrender.com`.

After that, every push to `main` redeploys the site.

## Ticket emails with free Gmail SMTP

The **Email tickets** admin tab reviews everyone with new ticket allocations and
sends one private, personalized message per recipient. A successful
`(email address, ticket number)` delivery is stored in the database, so uploading
the same complete export again does not send duplicates. If a returning buyer
gets more tickets, their next message highlights only the new tickets and also
lists all of their current tickets.

Sending is always manual: upload the complete spreadsheet, review and Save or
Merge the allocation, open **Email tickets**, review the recipient list, and
choose **Send all pending emails**.

No Microsoft tenant or paid email service is required. A normal Gmail account
can send through Gmail SMTP at no additional charge. For this event's roughly
80 recipient addresses, Gmail's normal personal-account daily sending limit is
sufficient. Google can change its limits and anti-spam policies, so send a test
message before the event.

### 1. Create a Gmail app password

1. Choose the Gmail account that will send the messages.
2. Turn on **2-Step Verification** for that Google account.
3. Open the Google Account **App passwords** page.
4. Create an app password named `Reverse Draw`.
5. Copy the generated 16-character password. This is `SMTP_PASSWORD`; do not
  use the account's normal Google password.

### 2. Set the environment variables

Use these exact names and values (replace the example account, password, name,
and URL):

```dotenv
EMAIL_PROVIDER=smtp
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_SECURITY=starttls
SMTP_USERNAME=your.sender.account@gmail.com
SMTP_PASSWORD=the-16-character-google-app-password
EMAIL_SENDER_ADDRESS=your.sender.account@gmail.com
EMAIL_SENDER_NAME=United Way Reverse Draw
PUBLIC_APP_URL=https://reverse-draw-xxxx.onrender.com
```

- `EMAIL_PROVIDER`: exactly `smtp`.
- `SMTP_HOST`: exactly `smtp.gmail.com` for Gmail.
- `SMTP_PORT`: exactly `587` when using `starttls`.
- `SMTP_SECURITY`: exactly `starttls`.
- `SMTP_USERNAME`: the full Gmail address used to sign in.
- `SMTP_PASSWORD`: the Google app password, with spaces removed.
- `EMAIL_SENDER_ADDRESS`: the address recipients see in **From**. Use the same
  address as `SMTP_USERNAME`, unless Gmail has already verified another Send As
  alias for this account. It falls back to `SMTP_USERNAME` if omitted.
- `EMAIL_SENDER_NAME`: the changeable display name recipients see, such as
  `United Way Reverse Draw`. It can be changed without changing credentials.
- `PUBLIC_APP_URL`: the deployed site's full `https://` origin, with no trailing
  slash and no `/admin` path.

For local development, store these lines in `.env` in the project root. That
file is excluded by `.gitignore` and must never be committed. Restart Uvicorn
after changing it.

For production, open the Render dashboard, select the web service, then open
**Environment** and add the same keys there. Render stores the secret values;
they do not belong in `app/config.py`, GitHub, or `render.yaml`. Save the changes
and let Render redeploy. To change the sender later, update
`EMAIL_SENDER_ADDRESS` and `EMAIL_SENDER_NAME` in Render and redeploy. Gmail will
not permit an arbitrary unverified From address.

A definite SMTP rejection remains pending for a later retry. A network timeout
after submission is marked **unknown** and blocked from automatic retry to avoid
a duplicate; check the Gmail Sent folder, then use **Mark delivered** or
**Allow retry** in the email batch history.

Microsoft Graph remains supported for existing deployments by setting
`EMAIL_PROVIDER=graph` together with `MS_GRAPH_TENANT_ID`, `MS_GRAPH_CLIENT_ID`,
`MS_GRAPH_CLIENT_SECRET`, `EMAIL_SENDER_ADDRESS`, and `PUBLIC_APP_URL`.

### 4. Before the event
- The free Render plan puts the app to sleep after 15 minutes without visitors, and the next visit waits while it wakes up. For the week of the draw, switch the service to the **Starter** plan in Render, then switch back afterwards.
- Do a practice run: load the holders, run every round, then **Reset** (keep holders ticked).
- If the admin page ever shows a red "No database is connected" banner, `DATABASE_URL` isn't set, and nothing will survive a restart.

## How the draw works
- Each round picks the survivors uniformly at random from the tickets still in, using a fresh cryptographic seed. The seed is saved in the log.
- The entire draw is stored as one JSON row in Postgres. Every change locks that row, so a double-click or a second admin tab can't run the same round twice.
- **Undo** puts the last round's tickets back in. The undone round is kept in an audit trail on the Elimination log tab.
- By default, holder names are **not** exposed on the public page. The public lookup works by ticket number only, and the winner's name appears in the final announcement. To change this, use `PUBLIC_SHOW_HOLDER_NAMES` and `PUBLIC_SHOW_WINNER_NAME` in `config.py`.

## Holder CSV format
```
ticket,name
1,Jane Doe
2,"Smith, Bob"
```
Paste the list or load a file on the admin **Ticket holders** tab. **Save** replaces every holder with the list. **Merge** only adds or overwrites the tickets in the list.
