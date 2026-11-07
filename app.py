import os, re, datetime, decimal
import pymysql
from functools import wraps
from flask import Flask, g, jsonify, redirect, render_template, request, session
from werkzeug.security import check_password_hash, generate_password_hash
import db as dbm      # loads .env first, so keep it above mailer
import mailer
import recommender

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-only-change-me")
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")


# ---------- helpers ----------
def db():
    if "db" not in g:
        g.db = dbm.connect()  # autocommit; we open transactions explicitly with START TRANSACTION
    return g.db

@app.teardown_appcontext
def _close(_):
    d = g.pop("db", None)
    if d: d.close()

def _plain(r):
    """MySQL returns date/datetime/Decimal objects; turn them into the plain values the front end expects."""
    for k, v in r.items():
        if isinstance(v, datetime.datetime): r[k] = v.strftime("%Y-%m-%d %H:%M:%S")
        elif isinstance(v, datetime.date): r[k] = v.isoformat()
        elif isinstance(v, decimal.Decimal): r[k] = int(v) if v == v.to_integral_value() else float(v)
    return r

def rows(sql, args=None): return [_plain(r) for r in db().execute(sql, args).fetchall()]
def one(sql, args=None):
    r = db().execute(sql, args).fetchone()
    return _plain(r) if r else None

def dup_key(e, name):
    """True if e is a MySQL duplicate-key error (1062) on the named unique key."""
    return bool(e.args) and e.args[0] == 1062 and name in str(e)
def err(msg, code=400): return jsonify(error=msg), code

def login_required(f):
    @wraps(f)
    def w(*a, **k):
        if "uid" not in session: return err("Please log in first", 401)
        return f(*a, **k)
    return w

class Fail(Exception):
    def __init__(self, msg, code=400): self.msg, self.code = msg, code

def attach_skills(users):
    if not users: return users
    ids = [u["id"] for u in users]
    m = {}
    for r in rows(f"""SELECT us.user_id, s.id sid, s.name, s.category, us.level FROM user_skills us
                      JOIN skills s ON s.id = us.skill_id
                      WHERE us.user_id IN ({','.join(['%s'] * len(ids))}) ORDER BY us.level DESC""", ids):
        m.setdefault(r["user_id"], []).append({"id": r["sid"], "name": r["name"], "category": r["category"], "level": r["level"]})
    for u in users: u["skills"] = m.get(u["id"], [])
    return users

def clean_github(v):
    """Accept a username or a github.com link; always store a link that opens."""
    v = (v or "").strip().rstrip("/")
    if not v: return None
    if re.fullmatch(r"@?[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})", v): return "https://github.com/" + v.lstrip("@")
    m = re.fullmatch(r"(?:https?://)?(?:www\.)?github\.com/([A-Za-z0-9-]{1,39})(?:/.*)?", v)
    if m: return "https://github.com/" + m.group(1)
    raise ValueError("Enter your GitHub username or a github.com profile link")

@app.get("/")
def index(): return render_template("index.html")

@app.get("/login")
def login_page():
    """Target of the links in notification emails: log in, then land on the dashboard to accept or decline."""
    return redirect("/%sgoto=dash")


# ---------- auth ----------
@app.post("/api/register")
def register():
    d = request.json or {}
    if len((d.get("name") or "").strip()) < 2: return err("Enter your full name")
    if not (d.get("email") or "").strip(): return err("Email is required")
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", d["email"].strip()): return err("Enter a valid email address, like name@college.edu")
    if len(d.get("password") or "") < 8: return err("Password must be at least 8 characters")
    if not 0 <= int(d.get("hours_per_week") or 10) <= 80: return err("Hours per week must be between 0 and 80")
    if not d.get("skills"): return err("Add at least one skill")
    try: gh = clean_github(d.get("github_url"))
    except ValueError as e: return err(str(e))
    c = db(); c.execute("START TRANSACTION")
    try:
        uid = c.execute("""INSERT INTO users(name,email,password_hash,gender,grad_year,branch,github_url,bio,availability,hours_per_week)
                           VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (d["name"].strip(), d["email"].strip().lower(), generate_password_hash(d["password"]), d.get("gender") or None,
             d.get("grad_year") or None, d.get("branch"), gh, d.get("bio"),
             d.get("availability") or "Flexible", d.get("hours_per_week") or 10)).lastrowid
        c.executemany("INSERT INTO user_skills VALUES(%s,%s,%s) ON DUPLICATE KEY UPDATE level=VALUES(level)",
                      [(uid, int(s["id"]), max(1, min(5, int(s.get("level", 3))))) for s in d.get("skills", [])])
        c.execute("COMMIT")
    except pymysql.err.MySQLError as e:
        c.execute("ROLLBACK")
        if dup_key(e, "uq_users_email"): return err("That email is already registered", 409)
        if e.args[0] in (3819, 4025): return err("One of the values is out of range (check graduation year and hours per week)", 400)
        raise
    except Exception:
        c.execute("ROLLBACK"); raise
    session["uid"] = uid
    return jsonify(ok=True)

@app.post("/api/login")
def login():
    d = request.json or {}
    u = one("SELECT id, password_hash FROM users WHERE email=%s", ((d.get("email") or "").strip().lower(),))
    if not u or not check_password_hash(u["password_hash"], d.get("password") or ""): return err("Wrong email or password", 401)
    session["uid"] = u["id"]
    return jsonify(ok=True)

@app.post("/api/logout")
def logout(): session.clear(); return jsonify(ok=True)

@app.get("/api/me")
def me():
    if "uid" not in session: return jsonify(user=None)
    u = one("SELECT id,name,email,gender,grad_year,branch,github_url,availability,hours_per_week FROM users WHERE id=%s", (session["uid"],))
    if not u: session.clear(); return jsonify(user=None)
    attach_skills([u])
    u["led_teams"] = rows("""SELECT t.id, t.name, t.event_id, e.title event_title FROM teams t JOIN events e ON e.id=t.event_id
                             WHERE t.leader_id=%s AND t.status='open'""", (u["id"],))
    return jsonify(user=u)


# ---------- reference data ----------
@app.get("/api/skills")
def skills(): return jsonify(rows("SELECT id,name,category FROM skills ORDER BY category,name"))

@app.get("/api/events")
def events():
    return jsonify(rows("""SELECT e.*, COUNT(DISTINCT t.id) teams,
                             COUNT(DISTINCT CASE WHEN t.status='open' THEN t.id END) open_teams
                           FROM events e LEFT JOIN teams t ON t.event_id=e.id GROUP BY e.id ORDER BY e.start_date"""))

def _fill_teams(ts):
    for t in ts:
        t["required"] = rows("SELECT s.id,s.name FROM team_required_skills r JOIN skills s ON s.id=r.skill_id WHERE r.team_id=%s", (t["id"],))
        t["members"] = rows("SELECT u.id,u.name,tm.role FROM team_members tm JOIN users u ON u.id=tm.user_id WHERE tm.team_id=%s", (t["id"],))
    return ts

@app.get("/api/events/<int:eid>/teams")
def event_teams(eid):
    return jsonify(_fill_teams(rows("""SELECT t.*, u.name leader_name FROM teams t JOIN users u ON u.id=t.leader_id
                 WHERE t.event_id=%s ORDER BY (t.status='open') DESC, t.id DESC""", (eid,))))

@app.get("/api/teams/search")
def search_teams():
    """Find teams by (part of) their name, across all events."""
    q = (request.args.get("q") or "").strip()
    if not q: return jsonify([])
    like = "%" + q.replace("!", "!!").replace("%", "!%").replace("_", "!_") + "%"   # escape char is !
    return jsonify(_fill_teams(rows("""SELECT t.*, u.name leader_name, e.title event_title FROM teams t
                 JOIN users u ON u.id=t.leader_id JOIN events e ON e.id=t.event_id
                 WHERE t.name LIKE %s ESCAPE '!' ORDER BY (t.status='open') DESC, t.name LIMIT 30""", (like,))))


# ---------- multi-filter teammate search (the JOIN-heavy query) ----------
@app.get("/api/users/search")
def search_users():
    a = request.args
    join_sql, join_args, where, args = "", [], ["1=1"], []
    for col in ("gender", "grad_year", "branch", "availability"):
        if a.get(col): where.append(f"u.{col} = %s"); args.append(a[col])
    if "uid" in session: where.append("u.id != %s"); args.append(session["uid"])  # don't list yourself
    if a.get("min_hours"): where.append("u.hours_per_week >= %s"); args.append(int(a["min_hours"]))
    if a.get("q"): where.append("(u.name LIKE %s OR u.bio LIKE %s)"); args += [f"%{a['q']}%"] * 2
    if a.get("event_id"):  # free agents only
        where.append("NOT EXISTS (SELECT 1 FROM team_members tm WHERE tm.user_id=u.id AND tm.event_id=%s)"); args.append(int(a["event_id"]))
    wanted = [int(x) for x in a.get("skills", "").split(",") if x.strip().isdigit()]
    having, matched = "", "0"
    if wanted:
        join_sql = f"JOIN user_skills us ON us.user_id=u.id AND us.skill_id IN ({','.join(['%s'] * len(wanted))}) AND us.level >= %s"
        join_args = wanted + [int(a.get("min_level", 1))]
        matched = "COUNT(DISTINCT us.skill_id)"
        if a.get("match", "all") == "all": having = f"HAVING COUNT(DISTINCT us.skill_id) = {len(wanted)}"
    sql = f"""SELECT u.id,u.name,u.gender,u.grad_year,u.branch,u.github_url,u.availability,u.hours_per_week,
                     {matched} AS matched
              FROM users u {join_sql} WHERE {' AND '.join(where)}
              GROUP BY u.id {having} ORDER BY matched DESC, u.name LIMIT 50"""
    return jsonify(attach_skills(rows(sql, join_args + args)))


# ---------- smart recommendations ----------
@app.get("/api/teams/<int:tid>/recommendations")
@login_required
def rec_users(tid):
    t = one("SELECT leader_id FROM teams WHERE id=%s", (tid,))
    if not t: return err("Team not found", 404)
    if t["leader_id"] != session["uid"]: return err("Only the team leader can see recommendations", 403)
    return jsonify(recommender.recommend_users(db(), tid, 5))

@app.get("/api/events/<int:eid>/recommended-teams")
@login_required
def rec_teams(eid): return jsonify(recommender.recommend_teams(db(), session["uid"], eid, 5))


# ---------- teams ----------
@app.post("/api/teams")
@login_required
def create_team():
    d = request.json or {}
    if not d.get("name") or not d.get("event_id"): return err("Team name and event are required")
    c = db(); c.execute("START TRANSACTION")
    try:
        tid = c.execute("INSERT INTO teams(event_id,name,description,leader_id,max_size) VALUES(%s,%s,%s,%s,%s)",
                        (d["event_id"], d["name"].strip(), d.get("description"), session["uid"],
                         max(2, min(10, int(d.get("max_size", 4)))))).lastrowid
        c.execute("INSERT INTO team_members(team_id,user_id,event_id,role) VALUES(%s,%s,%s, 'leader')", (tid, session["uid"], d["event_id"]))
        c.executemany("INSERT IGNORE INTO team_required_skills VALUES(%s,%s)", [(tid, int(s)) for s in d.get("skills", [])])
        c.execute("COMMIT")
    except pymysql.err.MySQLError as e:
        c.execute("ROLLBACK")
        if dup_key(e, "uq_event_user"): return err("You are already in a team for this event", 409)
        if dup_key(e, "uq_team_event_name"): return err("A team with that name already exists in this event", 409)
        if e.args[0] == 1452: return err("Unknown event", 400)
        raise
    except Exception:
        c.execute("ROLLBACK"); raise
    return jsonify(id=tid)


@app.post("/api/teams/<int:tid>/leave")
@login_required
def leave_team(tid):
    """Leave a team. A leader who leaves hands leadership to the longest-standing member,
       or disbands the team if they were the only one. All steps commit together or not at all."""
    c, uid = db(), session["uid"]
    c.execute("START TRANSACTION")
    try:
        c.execute("SELECT id FROM teams WHERE id=%s FOR UPDATE", (tid,))
        m = c.execute("SELECT role FROM team_members WHERE team_id=%s AND user_id=%s", (tid, uid)).fetchone()
        if not m: raise Fail("You are not in this team", 404)
        msg = "You left the team"
        if m["role"] == "leader":
            nxt = c.execute("SELECT user_id FROM team_members WHERE team_id=%s AND user_id!=%s ORDER BY joined_at, user_id LIMIT 1", (tid, uid)).fetchone()
            if nxt is None:
                c.execute("DELETE FROM teams WHERE id=%s", (tid,))   # cascades to members, skills, requests
                msg = "You were the only member, so the team was disbanded"
            else:
                c.execute("UPDATE team_members SET role='leader' WHERE team_id=%s AND user_id=%s", (tid, nxt["user_id"]))
                c.execute("UPDATE teams SET leader_id=%s WHERE id=%s", (nxt["user_id"], tid))
                c.execute("DELETE FROM team_members WHERE team_id=%s AND user_id=%s", (tid, uid))
                msg = "You left the team. Leadership passed to the longest-standing member"
        else:
            c.execute("DELETE FROM team_members WHERE team_id=%s AND user_id=%s", (tid, uid))
        c.execute("COMMIT")
    except Fail as e:
        c.execute("ROLLBACK"); return err(e.msg, e.code)
    except Exception:
        c.execute("ROLLBACK"); raise
    return jsonify(ok=True, message=msg)

@app.post("/api/teams/<int:tid>/kick")
@login_required
def kick_member(tid):
    t = one("SELECT leader_id FROM teams WHERE id=%s", (tid,))
    if not t: return err("Team not found", 404)
    if t["leader_id"] != session["uid"]: return err("Only the team leader can remove members", 403)
    uid = int((request.json or {}).get("user_id", 0))
    if uid == session["uid"]: return err("Use “Leave team” to leave your own team", 400)
    n = db().execute("DELETE FROM team_members WHERE team_id=%s AND user_id=%s AND role!='leader'", (tid, uid)).rowcount
    return jsonify(ok=True) if n else err("That student is not a member of this team", 404)


# ---------- requests & invites ----------
def _notify(t, user_id, kind, message):
    """Email the person who has to respond: the leader (join request) or the invited student (invite)."""
    try:
        event = one("SELECT title FROM events WHERE id=%s", (t["event_id"],))["title"]
        leader = one("SELECT name, email FROM users WHERE id=%s", (t["leader_id"],))
        person = one("SELECT name, email FROM users WHERE id=%s", (user_id,))
        link = os.environ.get("APP_URL", "http://127.0.0.1:5000").rstrip("/") + "/login"
        if kind == "request":
            to, subject = leader["email"], f"{person['name']} wants to join your team {t['name']}"
            body = (f"Hi {leader['name']},\n\n{person['name']} has asked to join your team \"{t['name']}\" for {event}.\n"
                    + (f"Their message: {message}\n" if message else "")
                    + f"\nReview the request and accept or decline it here (you will be asked to log in first):\n{link}\n\n- TeamUp")
        else:
            to, subject = person["email"], f"{leader['name']} invited you to join {t['name']}"
            body = (f"Hi {person['name']},\n\n{leader['name']} has invited you to join the team \"{t['name']}\" for {event}.\n"
                    f"\nAccept or decline the invite here (you will be asked to log in first):\n{link}\n\n- TeamUp")
        mailer.send_async(to, subject, body)
    except Exception as e:
        print(f"[mail] could not prepare notification: {e}")

def _create_request(tid, user_id, kind, message):
    t = one("SELECT * FROM teams WHERE id=%s", (tid,))
    if not t: return err("Team not found", 404)
    if t["status"] != "open": return err("This team is not accepting members", 409)
    if one("SELECT 1 x FROM team_members WHERE event_id=%s AND user_id=%s", (t["event_id"], user_id)):
        return err("Already in a team for this event", 409)
    try:
        db().execute("INSERT INTO join_requests(team_id,user_id,kind,message) VALUES(%s,%s,%s,%s)", (tid, user_id, kind, (message or "")[:300]))
    except pymysql.err.IntegrityError as e:
        if dup_key(e, "uq_pending_request"): return err("A request is already pending", 409)
        if e.args[0] == 1452: return err("That student does not exist", 404)
        raise
    _notify(t, user_id, kind, (message or "")[:300])
    return jsonify(ok=True)

@app.post("/api/teams/<int:tid>/join")
@login_required
def join(tid): return _create_request(tid, session["uid"], "request", (request.json or {}).get("message"))

@app.post("/api/teams/<int:tid>/invite")
@login_required
def invite(tid):
    t = one("SELECT leader_id FROM teams WHERE id=%s", (tid,))
    if not t or t["leader_id"] != session["uid"]: return err("Only the team leader can invite", 403)
    return _create_request(tid, int((request.json or {}).get("user_id", 0)), "invite", "")

@app.post("/api/requests/<int:rid>/respond")
@login_required
def respond(rid):
    """THE transaction: accept a request/invite atomically.
       1) lock  2) insert member (trigger bumps team count / marks full)  3) mark accepted
       4) close the user's other pending requests in the same event  5) if team full, close its remaining requests."""
    action = (request.json or {}).get("action")
    if action not in ("accept", "reject"): return err("Invalid action")
    c, uid = db(), session["uid"]
    c.execute("START TRANSACTION")  # row locks (FOR UPDATE below) stop two leaders grabbing the last seat
    try:
        r = c.execute("""SELECT jr.*, t.leader_id, t.event_id FROM join_requests jr JOIN teams t ON t.id=jr.team_id
                         WHERE jr.id=%s FOR UPDATE""", (rid,)).fetchone()   # row-locks the request AND its team
        if not r or r["status"] != "pending": raise Fail("This request is no longer pending", 409)
        responder = r["leader_id"] if r["kind"] == "request" else r["user_id"]
        if uid != responder: raise Fail("You can't respond to this request", 403)
        if action == "reject":
            c.execute("UPDATE join_requests SET status='rejected', resolved_at=CURRENT_TIMESTAMP WHERE id=%s", (rid,))
        else:
            c.execute("INSERT INTO team_members(team_id,user_id,event_id,role) VALUES(%s,%s,%s, 'member')", (r["team_id"], r["user_id"], r["event_id"]))
            c.execute("UPDATE join_requests SET status='accepted', resolved_at=CURRENT_TIMESTAMP WHERE id=%s", (rid,))
            c.execute("""UPDATE join_requests SET status='closed', resolved_at=CURRENT_TIMESTAMP
                         WHERE user_id=%s AND status='pending' AND team_id IN (SELECT id FROM teams WHERE event_id=%s)""", (r["user_id"], r["event_id"]))
            c.execute("""UPDATE join_requests SET status='closed', resolved_at=CURRENT_TIMESTAMP
                         WHERE team_id=%s AND status='pending' AND (SELECT status FROM teams WHERE id=%s)='full'""", (r["team_id"], r["team_id"]))
        c.execute("COMMIT")
    except Fail as e:
        c.execute("ROLLBACK"); return err(e.msg, e.code)
    except pymysql.err.MySQLError as e:
        c.execute("ROLLBACK")
        if e.args[0] == 1644 and "full" in str(e): return err("Sorry, the team just filled up", 409)   # raised by trg_block_full_team
        if dup_key(e, "uq_event_user"): return err("That student already joined another team in this event", 409)
        raise
    except Exception:
        c.execute("ROLLBACK"); raise
    return jsonify(ok=True)

@app.post("/api/requests/<int:rid>/withdraw")
@login_required
def withdraw(rid):
    n = db().execute("""UPDATE join_requests SET status='withdrawn', resolved_at=CURRENT_TIMESTAMP
        WHERE id=%s AND status='pending' AND ((kind='request' AND user_id=%s) OR
              (kind='invite' AND team_id IN (SELECT id FROM teams WHERE leader_id=%s)))""", (rid, session["uid"], session["uid"])).rowcount
    return jsonify(ok=True) if n else err("Nothing to withdraw", 404)


# ---------- dashboard & analytics ----------
@app.get("/api/my")
@login_required
def my():
    p = {"me": session["uid"]}
    base = """SELECT jr.id, jr.kind, jr.message, jr.created_at, t.id team_id, t.name team_name, e.title event_title,
                     u.id user_id, u.name user_name, l.name leader_name
              FROM join_requests jr JOIN teams t ON t.id=jr.team_id JOIN events e ON e.id=t.event_id
              JOIN users u ON u.id=jr.user_id JOIN users l ON l.id=t.leader_id WHERE jr.status='pending' AND """
    return jsonify(
        teams=rows("""SELECT t.id,t.name,t.status,t.current_size,t.max_size,e.title event_title,tm.role FROM team_members tm
                      JOIN teams t ON t.id=tm.team_id JOIN events e ON e.id=t.event_id WHERE tm.user_id=%s""", (session["uid"],)),
        incoming=rows(base + "((jr.kind='request' AND t.leader_id=%(me)s) OR (jr.kind='invite' AND jr.user_id=%(me)s)) ORDER BY jr.id DESC", p),
        outgoing=rows(base + "((jr.kind='request' AND jr.user_id=%(me)s) OR (jr.kind='invite' AND t.leader_id=%(me)s)) ORDER BY jr.id DESC", p))

@app.get("/api/analytics")
def analytics():
    return jsonify(
        skill_gap=rows("SELECT name, category, demand, supply, demand - supply AS gap FROM v_skill_gap WHERE demand > 0 ORDER BY demand DESC, gap DESC LIMIT 10"),
        events=rows("""SELECT e.title, COUNT(DISTINCT t.id) teams, COALESCE(SUM(t.current_size),0) placed,
                         (SELECT COUNT(*) FROM users u WHERE u.id NOT IN (SELECT user_id FROM team_members WHERE event_id=e.id)) free_agents
                       FROM events e LEFT JOIN teams t ON t.event_id=e.id GROUP BY e.id ORDER BY e.start_date"""),
        by_year=rows("SELECT grad_year, COUNT(*) n FROM users GROUP BY grad_year ORDER BY grad_year"))

if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1", port=int(os.environ.get("PORT", 5000)))
