import calendar, csv, io, math, os, re, sqlite3
from datetime import date, datetime, timedelta
from functools import wraps
from flask import (Flask, g, render_template, request, redirect,
                   url_for, session, flash, Response)
from werkzeug.security import generate_password_hash, check_password_hash

BASE = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, template_folder=os.path.join(BASE, "templates"))
app.secret_key = os.environ.get("SECRET_KEY", "dev-only-change-me")
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_SECURE=os.environ.get("PRODUCTION") == "1")
app.permanent_session_lifetime = timedelta(minutes=15)   # auto sign-out when idle
DB = os.path.join(BASE, "bank.db")

CATEGORIES = ["Food", "Travel", "Shopping", "Bills", "Rent", "Education",
              "Health", "Entertainment", "Other"]
TYPES = ("deposit", "expense", "saving", "unsave", "transfer_in", "transfer_out")
PLUS = ("deposit", "unsave", "transfer_in")

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  account_number TEXT NOT NULL UNIQUE,
  pin_hash TEXT NOT NULL,
  failed INTEGER NOT NULL DEFAULT 0,
  locked_until TEXT,
  roundup INTEGER NOT NULL DEFAULT 0,
  low_balance REAL NOT NULL DEFAULT 500,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS transactions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id INTEGER NOT NULL,
  type TEXT NOT NULL CHECK(type IN
    ('deposit','expense','saving','unsave','transfer_in','transfer_out')),
  category TEXT NOT NULL,
  purpose TEXT,
  amount REAL NOT NULL CHECK(amount > 0),
  date TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS budgets(
  account_id INTEGER NOT NULL,
  category TEXT NOT NULL,
  limit_amount REAL NOT NULL,
  PRIMARY KEY(account_id, category));
CREATE TABLE IF NOT EXISTS goals(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  target REAL NOT NULL,
  saved REAL NOT NULL DEFAULT 0,
  deadline TEXT);
CREATE TABLE IF NOT EXISTS recurring(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id INTEGER NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('deposit','expense')),
  category TEXT NOT NULL,
  purpose TEXT NOT NULL,
  amount REAL NOT NULL,
  day INTEGER NOT NULL,
  last_run TEXT);
CREATE TABLE IF NOT EXISTS beneficiaries(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  account_number TEXT NOT NULL,
  UNIQUE(account_id, account_number));
"""

# ---------------- database helpers ----------------
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB)
        g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close_db(_):
    db = g.pop("db", None)
    if db:
        db.close()

def init_db():
    with sqlite3.connect(DB) as c:
        c.executescript(SCHEMA)
        # upgrade an older database without losing data
        cols = {r[1] for r in c.execute("PRAGMA table_info(accounts)")}
        if "roundup" not in cols:
            c.execute("ALTER TABLE accounts ADD COLUMN roundup INTEGER NOT NULL DEFAULT 0")
        if "low_balance" not in cols:
            c.execute("ALTER TABLE accounts ADD COLUMN low_balance REAL NOT NULL DEFAULT 500")

@app.template_filter("inr")
def inr(v):
    return f"₹{v:,.2f}"

def login_required(f):
    @wraps(f)
    def wrap(*a, **k):
        aid = session.get("aid")
        if not aid or not get_db().execute(
                "SELECT 1 FROM accounts WHERE id=?", (aid,)).fetchone():
            session.clear()
            return redirect(url_for("login"))
        return f(*a, **k)
    return wrap

def money(key):
    try:
        v = float(request.form.get(key, ""))
        return round(v, 2) if v > 0 else None
    except ValueError:
        return None

def this_month():
    return date.today().strftime("%Y-%m")

def account(aid):
    return get_db().execute("SELECT * FROM accounts WHERE id=?", (aid,)).fetchone()

def sums(aid, where="", args=()):
    rows = get_db().execute(
        "SELECT type, COALESCE(SUM(amount),0) s FROM transactions "
        "WHERE account_id=? " + where + " GROUP BY type", (aid, *args)).fetchall()
    return {r["type"]: r["s"] for r in rows}

def month_sums(aid, month=None):
    return sums(aid, "AND strftime('%Y-%m',date)=?", (month or this_month(),))

def balance(aid):
    t = sums(aid)
    return round(t.get("deposit", 0) + t.get("unsave", 0) + t.get("transfer_in", 0)
                 - t.get("expense", 0) - t.get("saving", 0) - t.get("transfer_out", 0), 2)

def savings_info(aid):
    t = sums(aid)
    total = round(t.get("saving", 0) - t.get("unsave", 0), 2)
    goals = get_db().execute("SELECT COALESCE(SUM(saved),0) FROM goals WHERE account_id=?",
                             (aid,)).fetchone()[0]
    return total, round(goals, 2), round(total - goals, 2)   # total, in goals, free

def add_txn(aid, ttype, category, purpose, amount, d=None):
    get_db().execute(
        "INSERT INTO transactions(account_id,type,category,purpose,amount,date) "
        "VALUES(?,?,?,?,?,?)",
        (aid, ttype, category, purpose, round(amount, 2), d or date.today().isoformat()))

def category_breakdown(aid, month):
    return get_db().execute(
        "SELECT category, SUM(amount) s FROM transactions WHERE account_id=? AND "
        "type='expense' AND strftime('%Y-%m',date)=? GROUP BY category ORDER BY s DESC",
        (aid, month)).fetchall()

def budget_status(aid):
    db = get_db()
    spent = {r["category"]: r["s"] for r in category_breakdown(aid, this_month())}
    out = []
    for b in db.execute("SELECT * FROM budgets WHERE account_id=?", (aid,)):
        s = spent.get(b["category"], 0)
        out.append({"category": b["category"], "limit": b["limit_amount"], "spent": s,
                    "pct": round(s / b["limit_amount"] * 100)})
    return out

def apply_recurring(aid):
    """Auto-post monthly recurring deposits/expenses once their day has come."""
    db, today, m = get_db(), date.today(), this_month()
    for r in db.execute("SELECT * FROM recurring WHERE account_id=?", (aid,)).fetchall():
        if today.day >= r["day"] and r["last_run"] != m:
            if r["kind"] == "expense" and r["amount"] > balance(aid):
                flash(f"Recurring '{r['purpose']}' skipped: insufficient balance.", "err")
                continue
            add_txn(aid, r["kind"], r["category"], r["purpose"] + " (recurring)", r["amount"])
            db.execute("UPDATE recurring SET last_run=? WHERE id=?", (m, r["id"]))
    db.commit()

# ---------------- insights ----------------
def month_list(n=6):
    y, m, out = date.today().year, date.today().month, []
    for _ in range(n):
        out.append(f"{y}-{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return out[::-1]

def trend(aid, n=6):
    rows = {r["mth"]: r for r in get_db().execute(
        "SELECT strftime('%Y-%m',date) mth, "
        "SUM(CASE WHEN type='deposit' THEN amount ELSE 0 END) inc, "
        "SUM(CASE WHEN type='expense' THEN amount ELSE 0 END) exp "
        "FROM transactions WHERE account_id=? GROUP BY mth", (aid,))}
    out = []
    for mth in month_list(n):
        r = rows.get(mth)
        out.append({"mth": mth, "label": calendar.month_abbr[int(mth[5:])],
                    "inc": r["inc"] if r else 0, "exp": r["exp"] if r else 0})
    return out

def health(aid):
    """Financial health score 0-100: savings rate (40) + budgets (30) + emergency fund (30)."""
    db, m = get_db(), month_sums(aid)
    inc, exp = m.get("deposit", 0), m.get("expense", 0)
    if not inc and not exp:
        return {"score": 0, "label": "No data yet", "color": "#94a3b8",
                "tips": ["Add a deposit and a few expenses to see your score."]}
    rate = (inc - exp) / inc if inc else 0
    bs = budget_status(aid)
    n_months = db.execute("SELECT COUNT(DISTINCT strftime('%Y-%m',date)) FROM transactions "
                          "WHERE account_id=? AND type='expense'", (aid,)).fetchone()[0]
    avg = sums(aid).get("expense", 0) / n_months if n_months else 0
    covered = savings_info(aid)[0] / avg if avg else 0
    p1 = max(0, min(rate / 0.2, 1)) * 40
    p2 = sum(1 for b in bs if b["pct"] < 100) / len(bs) * 30 if bs else 15
    p3 = min(covered / 3, 1) * 30 if avg else 15
    score = round(p1 + p2 + p3)
    tips = []
    if rate < 0.2:
        tips.append("Try to keep at least 20% of what you deposit each month.")
    if not bs:
        tips.append("Set monthly budgets to keep your spending in check.")
    elif any(b["pct"] >= 100 for b in bs):
        tips.append("You are over budget in some categories this month.")
    if avg and covered < 3:
        tips.append(f"Build an emergency fund of 3 months of spending (now {covered:.1f}).")
    label, color = (("Excellent", "#10b981") if score >= 75 else
                    ("Good", "#06b6d4") if score >= 50 else
                    ("Fair", "#f59e0b") if score >= 25 else ("Needs work", "#ef4444"))
    return {"score": score, "label": label, "color": color,
            "tips": tips[:3] or ["Great job! Keep it up."]}

def insight_list(aid):
    db, cur, prev, out = get_db(), this_month(), month_list(2)[0], []
    c = month_sums(aid, cur).get("expense", 0)
    p = month_sums(aid, prev).get("expense", 0)
    if c and p:
        ch = (c - p) / p * 100
        out.append(f"You spent {abs(ch):.0f}% {'more' if ch > 0 else 'less'} than last month.")
    cats = category_breakdown(aid, cur)
    if cats and c:
        out.append(f"{cats[0]['category']} is your top category "
                   f"({cats[0]['s'] / c * 100:.0f}% of spending).")
    big = db.execute("SELECT purpose, amount FROM transactions WHERE account_id=? AND "
                     "type='expense' AND strftime('%Y-%m',date)=? ORDER BY amount DESC LIMIT 1",
                     (aid, cur)).fetchone()
    if big:
        out.append(f"Biggest expense this month: {big['purpose']} (₹{big['amount']:,.0f}).")
    return out or ["Add a few transactions to unlock insights."]

def upcoming(aid):
    day, out = date.today().day, []
    for r in get_db().execute("SELECT * FROM recurring WHERE account_id=?", (aid,)):
        if r["last_run"] != this_month() and r["day"] > day:
            out.append({"purpose": r["purpose"], "kind": r["kind"],
                        "amount": r["amount"], "in_days": r["day"] - day})
    return sorted(out, key=lambda x: x["in_days"])

# ---------------- auth ----------------
@app.route("/")
def home():
    return redirect(url_for("dashboard" if "aid" in session else "login"))

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        name = request.form["name"].strip()
        acc = request.form["account_number"].strip()
        pin = request.form["pin"].strip()
        opening = request.form.get("opening") or "0"
        if not name or not re.fullmatch(r"\d{9,16}", acc):
            flash("Name required; account number must be 9-16 digits.", "err")
        elif not re.fullmatch(r"\d{4,6}", pin):
            flash("PIN must be 4-6 digits.", "err")
        else:
            try:
                db = get_db()
                cur = db.execute(
                    "INSERT INTO accounts(name,account_number,pin_hash) VALUES(?,?,?)",
                    (name, acc, generate_password_hash(pin)))
                if float(opening) > 0:
                    add_txn(cur.lastrowid, "deposit", "Opening", "Opening balance", float(opening))
                db.commit()
                flash("Account created. Please log in.", "ok")
                return redirect(url_for("login"))
            except sqlite3.IntegrityError:
                flash("That account number already exists.", "err")
            except ValueError:
                flash("Opening balance must be a number.", "err")
    return render_template("register.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        db, now = get_db(), datetime.now()
        row = db.execute("SELECT * FROM accounts WHERE account_number=?",
                         (request.form["account_number"].strip(),)).fetchone()
        if row and row["locked_until"] and datetime.fromisoformat(row["locked_until"]) > now:
            flash("Account locked for 5 minutes after 3 wrong PINs.", "err")
        elif row and check_password_hash(row["pin_hash"], request.form["pin"]):
            db.execute("UPDATE accounts SET failed=0, locked_until=NULL WHERE id=?", (row["id"],))
            db.commit()
            session.clear()
            session.permanent = True
            session["aid"], session["name"] = row["id"], row["name"]
            return redirect(url_for("dashboard"))
        else:
            if row:
                f = row["failed"] + 1
                lock = (now + timedelta(minutes=5)).isoformat() if f >= 3 else None
                db.execute("UPDATE accounts SET failed=?, locked_until=? WHERE id=?",
                           (0 if f >= 3 else f, lock, row["id"]))
                db.commit()
            flash("Wrong account number or PIN.", "err")
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# ---------------- home (dashboard) ----------------
@app.route("/dashboard")
@login_required
def dashboard():
    aid, db = session["aid"], get_db()
    apply_recurring(aid)
    acct = account(aid)
    bal = balance(aid)
    m = month_sums(aid)
    dep, exp = m.get("deposit", 0), m.get("expense", 0)
    alerts = []
    for b in budget_status(aid):
        if b["pct"] >= 100:
            alerts.append(f"Over budget on {b['category']} ({b['pct']}%)")
        elif b["pct"] >= 80:
            alerts.append(f"{b['category']} budget is {b['pct']}% used")
    if acct["low_balance"] > 0 and bal < acct["low_balance"]:
        alerts.append(f"Low balance: less than ₹{acct['low_balance']:,.0f} left.")
    goals = [{"name": g_["name"], "pct": min(round(g_["saved"] / g_["target"] * 100), 100)}
             for g_ in db.execute("SELECT * FROM goals WHERE account_id=? ORDER BY id DESC LIMIT 3", (aid,))]
    recent = db.execute("SELECT * FROM transactions WHERE account_id=? "
                        "ORDER BY date DESC, id DESC LIMIT 6", (aid,)).fetchall()
    cats = category_breakdown(aid, this_month())
    return render_template(
        "dashboard.html", acct=acct, bal=bal, s_total=savings_info(aid)[0], dep=dep, exp=exp,
        profit=dep - exp, h=health(aid), insights=insight_list(aid), upcoming=upcoming(aid),
        goals=goals, recent=recent, trend=trend(aid), plus=PLUS,
        cat_labels=[c["category"] for c in cats], cat_values=[c["s"] for c in cats],
        alerts=alerts)

# ---------------- deposit ----------------
@app.route("/deposit")
@login_required
def deposit_page():
    aid, db = session["aid"], get_db()
    rows = db.execute("SELECT * FROM transactions WHERE account_id=? AND type='deposit' "
                      "ORDER BY date DESC, id DESC LIMIT 30", (aid,)).fetchall()
    today = db.execute("SELECT COALESCE(SUM(amount),0) FROM transactions WHERE account_id=? "
                       "AND type='deposit' AND date=?", (aid, date.today().isoformat())).fetchone()[0]
    return render_template("deposit.html", rows=rows, bal=balance(aid),
                           total=sums(aid).get("deposit", 0),
                           month=month_sums(aid).get("deposit", 0), today=today,
                           today_date=date.today().isoformat())

@app.route("/deposit/add", methods=["POST"])
@login_required
def deposit_add():
    amt = money("amount")
    if amt is None:
        flash("Enter a valid amount.", "err")
    else:
        add_txn(session["aid"], "deposit", "Deposit",
                request.form.get("purpose", "").strip() or "Deposit", amt,
                request.form.get("date") or None)
        get_db().commit()
        flash(f"₹{amt:,.2f} deposited.", "ok")
    return redirect(url_for("deposit_page"))

# ---------------- expenses ----------------
@app.route("/expenses")
@login_required
def expenses_page():
    aid, db = session["aid"], get_db()
    month = request.args.get("month") or this_month()
    cat = request.args.get("category", "")
    sql = ("SELECT * FROM transactions WHERE account_id=? AND type='expense' "
           "AND strftime('%Y-%m',date)=?")
    args = [aid, month]
    if cat in CATEGORIES:
        sql += " AND category=?"
        args.append(cat)
    rows = db.execute(sql + " ORDER BY date DESC, id DESC", args).fetchall()
    shown = category_breakdown(aid, month)
    cur = category_breakdown(aid, this_month())
    today = date.today()
    spent = sum(c["s"] for c in cur)
    avg = spent / today.day
    days = calendar.monthrange(today.year, today.month)[1]
    return render_template(
        "expenses.html", rows=rows, total=sum(r["amount"] for r in rows),
        labels=[c["category"] for c in shown], values=[c["s"] for c in shown],
        top=cur[0] if cur else None, avg=avg, projected=avg * days,
        budgets=budget_status(aid), categories=CATEGORIES, month=month, cat=cat,
        bal=balance(aid), today_date=today.isoformat())

@app.route("/expenses/add", methods=["POST"])
@login_required
def expense_add():
    aid, amt = session["aid"], money("amount")
    cat = request.form.get("category", "Other")
    purpose = request.form.get("purpose", "").strip()
    if amt is None or cat not in CATEGORIES:
        flash("Enter a valid amount and category.", "err")
    elif not purpose:
        flash("Please enter the purpose of this expense.", "err")
    elif amt > balance(aid):
        flash("Insufficient balance.", "err")
    else:
        add_txn(aid, "expense", cat, purpose, amt, request.form.get("date") or None)
        msg = "Expense saved."
        if account(aid)["roundup"]:   # round-up savings: spare change goes to savings
            extra = round(math.ceil(amt / 10) * 10 - amt, 2)
            if 0 < extra <= balance(aid):
                add_txn(aid, "saving", "Round-up", f"Round-up on {purpose}", extra)
                msg = f"Expense saved. ₹{extra:,.2f} round-up moved to savings."
        get_db().commit()
        flash(msg, "ok")
    return redirect(url_for("expenses_page"))

@app.route("/expenses/delete/<int:tid>", methods=["POST"])
@login_required
def expense_delete(tid):
    db = get_db()
    db.execute("DELETE FROM transactions WHERE id=? AND account_id=? AND type='expense'",
               (tid, session["aid"]))
    db.commit()
    return redirect(url_for("expenses_page"))

@app.route("/budget", methods=["POST"])
@login_required
def budget_set():
    amt, cat = money("limit"), request.form.get("category")
    if amt is None or cat not in CATEGORIES:
        flash("Invalid budget.", "err")
    else:
        db = get_db()
        db.execute("INSERT OR REPLACE INTO budgets VALUES(?,?,?)", (session["aid"], cat, amt))
        db.commit()
        flash("Budget saved.", "ok")
    return redirect(url_for("expenses_page"))

# ---------------- savings ----------------
@app.route("/savings")
@login_required
def savings_page():
    aid, db = session["aid"], get_db()
    total, in_goals, free = savings_info(aid)
    m = month_sums(aid)
    saved_m = m.get("saving", 0) - m.get("unsave", 0)
    dep = m.get("deposit", 0)
    rows = db.execute("SELECT * FROM transactions WHERE account_id=? AND type IN "
                      "('saving','unsave') ORDER BY date DESC, id DESC LIMIT 30", (aid,)).fetchall()
    return render_template("savings.html", total=total, in_goals=in_goals, free=free,
                           saved_m=saved_m, rate=round(saved_m / dep * 100, 1) if dep else 0,
                           rows=rows, bal=balance(aid))

@app.route("/savings/add", methods=["POST"])
@login_required
def savings_add():
    aid, amt = session["aid"], money("amount")
    if amt is None:
        flash("Enter a valid amount.", "err")
    elif amt > balance(aid):
        flash("Insufficient balance.", "err")
    else:
        add_txn(aid, "saving", "Savings", "Moved to savings", amt)
        get_db().commit()
        flash("Moved to savings.", "ok")
    return redirect(url_for("savings_page"))

@app.route("/savings/withdraw", methods=["POST"])
@login_required
def savings_withdraw():
    aid, amt = session["aid"], money("amount")
    if amt is None:
        flash("Enter a valid amount.", "err")
    elif amt > savings_info(aid)[2]:
        flash("Not enough free savings (money inside goals is locked).", "err")
    else:
        add_txn(aid, "unsave", "Savings", "Withdrawn from savings", amt)
        get_db().commit()
        flash("Withdrawn to balance.", "ok")
    return redirect(url_for("savings_page"))

# ---------------- profit ----------------
@app.route("/profit")
@login_required
def profit_page():
    aid = session["aid"]
    rows = get_db().execute(
        "SELECT strftime('%Y-%m',date) mth, "
        "SUM(CASE WHEN type='deposit' THEN amount ELSE 0 END) inc, "
        "SUM(CASE WHEN type='expense' THEN amount ELSE 0 END) exp "
        "FROM transactions WHERE account_id=? GROUP BY mth ORDER BY mth DESC LIMIT 12",
        (aid,)).fetchall()
    months = []
    for r in rows:
        p = r["inc"] - r["exp"]
        months.append({"mth": r["mth"], "inc": r["inc"], "exp": r["exp"], "profit": p,
                       "rate": round(p / r["inc"] * 100, 1) if r["inc"] else 0})
    best = max(months, key=lambda x: x["profit"]) if months else None
    return render_template("profit.html", months=months, chart=months[::-1], best=best,
                           cur=next((x for x in months if x["mth"] == this_month()), None),
                           total=sum(x["profit"] for x in months))

# ---------------- goals ----------------
@app.route("/goals")
@login_required
def goals_page():
    aid, out = session["aid"], []
    for g_ in get_db().execute("SELECT * FROM goals WHERE account_id=?", (aid,)):
        d = dict(g_)
        d["pct"] = min(round(d["saved"] / d["target"] * 100), 100)
        d["left"] = round(d["target"] - d["saved"], 2)
        d["per_month"] = None
        if d["deadline"] and d["left"] > 0:
            dl = date.fromisoformat(d["deadline"])
            months = max((dl.year - date.today().year) * 12 + dl.month - date.today().month, 1)
            d["per_month"] = d["left"] / months
            d["days"] = (dl - date.today()).days
        out.append(d)
    return render_template("goals.html", goals=out, bal=balance(aid))

@app.route("/goals/add", methods=["POST"])
@login_required
def goal_add():
    amt, name = money("target"), request.form.get("name", "").strip()
    if amt is None or not name:
        flash("Enter a goal name and target.", "err")
    else:
        db = get_db()
        db.execute("INSERT INTO goals(account_id,name,target,deadline) VALUES(?,?,?,?)",
                   (session["aid"], name, amt, request.form.get("deadline") or None))
        db.commit()
        flash("Goal created.", "ok")
    return redirect(url_for("goals_page"))

@app.route("/goals/<int:gid>/fund", methods=["POST"])
@login_required
def goal_fund(gid):
    aid, amt, db = session["aid"], money("amount"), get_db()
    goal = db.execute("SELECT * FROM goals WHERE id=? AND account_id=?", (gid, aid)).fetchone()
    if not goal or amt is None:
        flash("Invalid amount.", "err")
    elif amt > balance(aid):
        flash("Insufficient balance.", "err")
    elif amt > goal["target"] - goal["saved"] + 0.001:
        flash("That is more than the goal still needs.", "err")
    else:
        db.execute("UPDATE goals SET saved=saved+? WHERE id=?", (amt, gid))
        add_txn(aid, "saving", "Goal", f"Goal: {goal['name']}", amt)
        db.commit()
        flash("Goal funded.", "ok")
    return redirect(url_for("goals_page"))

@app.route("/goals/<int:gid>/delete", methods=["POST"])
@login_required
def goal_delete(gid):
    db = get_db()   # money stays in savings and becomes free again
    db.execute("DELETE FROM goals WHERE id=? AND account_id=?", (gid, session["aid"]))
    db.commit()
    flash("Goal removed. Its money is back in free savings.", "ok")
    return redirect(url_for("goals_page"))

# ---------------- transfer ----------------
@app.route("/transfer", methods=["GET", "POST"])
@login_required
def transfer_page():
    aid, db = session["aid"], get_db()
    if request.method == "POST":
        amt, to = money("amount"), request.form.get("to", "").strip()
        me = account(aid)
        rcv = db.execute("SELECT * FROM accounts WHERE account_number=?", (to,)).fetchone()
        if not check_password_hash(me["pin_hash"], request.form.get("pin", "")):
            flash("Wrong PIN.", "err")
        elif amt is None:
            flash("Enter a valid amount.", "err")
        elif not rcv or rcv["id"] == aid:
            flash("Receiver account not found.", "err")
        elif amt > balance(aid):
            flash("Insufficient balance.", "err")
        else:
            note = request.form.get("note", "").strip()
            add_txn(aid, "transfer_out", "Transfer", f"To {rcv['name']} {note}".strip(), amt)
            add_txn(rcv["id"], "transfer_in", "Transfer", f"From {me['name']} {note}".strip(), amt)
            if request.form.get("save_payee"):
                db.execute("INSERT OR IGNORE INTO beneficiaries(account_id,name,account_number) "
                           "VALUES(?,?,?)", (aid, rcv["name"], rcv["account_number"]))
            db.commit()
            flash(f"₹{amt:,.2f} sent to {rcv['name']}.", "ok")
        return redirect(url_for("transfer_page"))
    rows = db.execute("SELECT * FROM transactions WHERE account_id=? AND type IN "
                      "('transfer_in','transfer_out') ORDER BY date DESC, id DESC LIMIT 20",
                      (aid,)).fetchall()
    payees = db.execute("SELECT * FROM beneficiaries WHERE account_id=? ORDER BY name",
                        (aid,)).fetchall()
    return render_template("transfer.html", rows=rows, payees=payees, bal=balance(aid))

@app.route("/payee/<int:pid>/delete", methods=["POST"])
@login_required
def payee_delete(pid):
    db = get_db()
    db.execute("DELETE FROM beneficiaries WHERE id=? AND account_id=?", (pid, session["aid"]))
    db.commit()
    return redirect(url_for("transfer_page"))

# ---------------- recurring ----------------
@app.route("/recurring", methods=["GET", "POST"])
@login_required
def recurring_page():
    aid, db = session["aid"], get_db()
    if request.method == "POST":
        amt, purpose = money("amount"), request.form.get("purpose", "").strip()
        kind, cat = request.form.get("kind"), request.form.get("category", "Other")
        try:
            day = int(request.form.get("day", 1))
        except ValueError:
            day = 0
        if (amt is None or not purpose or kind not in ("deposit", "expense")
                or not 1 <= day <= 28 or cat not in CATEGORIES):
            flash("Fill all fields (day must be 1-28).", "err")
        else:
            db.execute("INSERT INTO recurring(account_id,kind,category,purpose,amount,day) "
                       "VALUES(?,?,?,?,?,?)", (aid, kind, cat, purpose, amt, day))
            db.commit()
            flash("Recurring item added. It posts automatically each month.", "ok")
        return redirect(url_for("recurring_page"))
    rows = db.execute("SELECT * FROM recurring WHERE account_id=?", (aid,)).fetchall()
    return render_template("recurring.html", rows=rows, categories=CATEGORIES)

@app.route("/recurring/<int:rid>/delete", methods=["POST"])
@login_required
def recurring_delete(rid):
    db = get_db()
    db.execute("DELETE FROM recurring WHERE id=? AND account_id=?", (rid, session["aid"]))
    db.commit()
    return redirect(url_for("recurring_page"))

# ---------------- statement ----------------
def filtered(aid):
    sql, args = "SELECT * FROM transactions WHERE account_id=?", [aid]
    f, t = request.args.get("from", ""), request.args.get("to", "")
    ty, q = request.args.get("type", ""), request.args.get("q", "").strip()
    if f:
        sql += " AND date>=?"
        args.append(f)
    if t:
        sql += " AND date<=?"
        args.append(t)
    if ty in TYPES:
        sql += " AND type=?"
        args.append(ty)
    if q:
        sql += " AND (purpose LIKE ? OR category LIKE ?)"
        args += [f"%{q}%", f"%{q}%"]
    return sql + " ORDER BY date DESC, id DESC", args

@app.route("/statement")
@login_required
def statement_page():
    sql, args = filtered(session["aid"])
    rows = get_db().execute(sql + " LIMIT 300", args).fetchall()
    money_in = sum(r["amount"] for r in rows if r["type"] in PLUS)
    money_out = sum(r["amount"] for r in rows if r["type"] not in PLUS)
    return render_template("statement.html", rows=rows, types=TYPES, plus=PLUS,
                           bal=balance(session["aid"]), money_in=money_in, money_out=money_out)

@app.route("/statement.csv")
@login_required
def statement_csv():
    sql, args = filtered(session["aid"])
    rows = get_db().execute(sql, args).fetchall()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Date", "Type", "Category", "Purpose", "Amount"])
    for r in rows:
        w.writerow([r["date"], r["type"], r["category"], r["purpose"], r["amount"]])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=statement.csv"})

# ---------------- tools & settings ----------------
@app.route("/tools")
@login_required
def tools_page():
    return render_template("tools.html")

@app.route("/settings")
@login_required
def settings_page():
    return render_template("settings.html", acct=account(session["aid"]))

@app.route("/settings/profile", methods=["POST"])
@login_required
def settings_profile():
    name = request.form.get("name", "").strip()
    if not name:
        flash("Name cannot be empty.", "err")
    else:
        db = get_db()
        db.execute("UPDATE accounts SET name=? WHERE id=?", (name, session["aid"]))
        db.commit()
        session["name"] = name
        flash("Name updated.", "ok")
    return redirect(url_for("settings_page"))

@app.route("/settings/pin", methods=["POST"])
@login_required
def settings_pin():
    me = account(session["aid"])
    new, conf = request.form.get("new", "").strip(), request.form.get("confirm", "").strip()
    if not check_password_hash(me["pin_hash"], request.form.get("current", "")):
        flash("Current PIN is wrong.", "err")
    elif not re.fullmatch(r"\d{4,6}", new):
        flash("New PIN must be 4-6 digits.", "err")
    elif new != conf:
        flash("The two new PINs do not match.", "err")
    else:
        db = get_db()
        db.execute("UPDATE accounts SET pin_hash=? WHERE id=?",
                   (generate_password_hash(new), me["id"]))
        db.commit()
        flash("PIN updated.", "ok")
    return redirect(url_for("settings_page"))

@app.route("/settings/prefs", methods=["POST"])
@login_required
def settings_prefs():
    try:
        low = max(float(request.form.get("low") or 0), 0)
    except ValueError:
        flash("Low balance alert must be a number.", "err")
        return redirect(url_for("settings_page"))
    db = get_db()
    db.execute("UPDATE accounts SET roundup=?, low_balance=? WHERE id=?",
               (1 if request.form.get("roundup") else 0, low, session["aid"]))
    db.commit()
    flash("Preferences saved.", "ok")
    return redirect(url_for("settings_page"))

# ---------------- help ----------------
@app.route("/help")
def help_page():
    return render_template("help.html")

# ---------------- close account ----------------
@app.route("/close-account", methods=["GET", "POST"])
@login_required
def close_account_page():
    aid, db = session["aid"], get_db()
    if request.method == "POST":
        me = account(aid)
        if not check_password_hash(me["pin_hash"], request.form.get("pin", "")):
            flash("Wrong PIN.", "err")
        elif request.form.get("confirm", "").strip().upper() != "CLOSE":
            flash("Type CLOSE to confirm.", "err")
        else:
            for table in ("transactions", "budgets", "goals", "recurring", "beneficiaries"):
                db.execute(f"DELETE FROM {table} WHERE account_id=?", (aid,))
            db.execute("DELETE FROM accounts WHERE id=?", (aid,))
            db.commit()
            session.clear()
            flash("Your account and all its data have been deleted.", "ok")
            return redirect(url_for("login"))
        return redirect(url_for("close_account_page"))
    return render_template("close_account.html", bal=balance(aid))

init_db()   # create tables on start (also needed when run by gunicorn / PythonAnywhere)

if __name__ == "__main__":
    app.run(debug=True)