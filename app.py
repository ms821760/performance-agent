import os
import re
import json
import requests
from datetime import date, timedelta
from flask import Flask, request, jsonify, render_template, session, redirect, url_for
from functools import wraps

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'change-me-in-production')

# ── Config ────────────────────────────────────────────────────
SUPABASE_URL  = os.environ.get('SUPABASE_URL')
SUPABASE_KEY  = os.environ.get('SUPABASE_KEY')
ANTHROPIC_KEY = os.environ.get('ANTHROPIC_API_KEY')
APP_USERNAME  = os.environ.get('APP_USERNAME', 'admin')
APP_PASSWORD  = os.environ.get('APP_PASSWORD', 'changeme')

TODAY = date.today().isoformat()

# ── Training phases ───────────────────────────────────────────
PHASES = [
    {
        "name":  "Body Comp + MS 150",
        "start": "2026-01-01",
        "end":   "2026-04-27",
        "goal":  "Get body fat below 15% while maintaining/building muscle mass to 105-110 lbs. MS 150 bike ride April 25-26.",
        "focus": ["body_composition", "cycling", "nutrition", "z2_zones"],
        "watch": ["body_fat_pct", "muscle_mass", "calorie_balance", "protein_intake"],
        "color": "#c8a96e"
    },
    {
        "name":  "Recovery + Aerobic Base",
        "start": "2026-04-28",
        "end":   "2026-08-31",
        "goal":  "Post MS-150 recovery. Gradual running reintroduction. Continue body composition improvement. Build aerobic base with easy Z2 running.",
        "focus": ["body_composition", "nutrition", "easy_running", "z2_zones"],
        "watch": ["body_fat_pct", "muscle_mass", "run_volume_ramp", "hrv", "nutrition"],
        "color": "#4a7c59"
    },
    {
        "name":  "Houston Marathon Build",
        "start": "2026-09-01",
        "end":   "2027-01-17",
        "goal":  "Train for Houston Marathon (Jan 17, 2027). Target finish ~4:30, priority is finishing healthy. History of overtraining — keep increases conservative.",
        "focus": ["running", "long_runs", "nutrition_adequacy", "recovery"],
        "watch": ["weekly_mileage", "long_run_distance", "hrv", "resting_hr", "calorie_intake"],
        "color": "#4a6fa5"
    }
]

ATHLETE_CONTEXT = f"""
ATHLETE PROFILE:
- Age: 47, Male, drilling engineer on sabbatical
- Today: {TODAY}
- History: overtraining prone, previously did keto (under-eating risk), needs structured progression

BODY COMPOSITION BASELINE (2026-03-26):
- Weight: 224.9 lbs | Body fat: 19.6% | Muscle mass: 104.1 lbs | InBody score: 99 | Visceral fat: 9

GOALS:
- Phase 1 (now -> Apr 27): Body fat <15%, muscle 105-110 lbs, complete MS 150 bike ride Apr 25-26
- Phase 2 (Apr 28 -> Aug 31): Maintain body comp gains, easy running reintroduction, aerobic base
- Phase 3 (Sep 1 -> Jan 17): Houston Marathon, target ~4:30, priority = finish healthy

CURRENT PRs:
- 400m: 1:35 | 1 mile: 8:09 | 5K: 27:20 | 10K: 58:05 | Half Marathon: 2:06:06 (Jan 2026)

HR ZONES: Z1 <130 | Z2 131-150 | Z3 151-160 | Z4 161-170 | Z5 >171 bpm

KEY COACHING RULES:
- Never increase weekly run mileage more than 10% per week
- Flag if nutrition calories are too low for training load (muscle loss risk)
- Flag if protein looks insufficient on high training days
- Z2 training is the priority for both fat loss and aerobic base building
- Excessive Z4/Z5 during body comp phase risks muscle catabolism
- Watch HRV and resting HR trends for overtraining signs
- Nutrition tracking reminder: flag days with no nutrition log
- DATA WINDOWS: weekly_summary = last 7 days, month_to_date = from 1st of current month to today.
  NEVER say "this month" based only on 7-day data. Always use month_to_date for monthly statements.
  If the month just started and month_to_date only covers a few days, say so explicitly.
"""

DB_SCHEMA = """
DATABASE SCHEMA (PostgreSQL):
- workouts_strava: activity_id, date, sport_type, name, moving_time_min, distance_miles, avg_hr, max_hr, calories, total_elevation_gain_m. sport_type: Run/Ride/GravelRide/VirtualRide/Workout/Strength/Cardio/Walk
- workouts_apple: workout_id, date, sport_type, distance_mi, duration_min, avg_pace_display, avg_hr_bpm, max_hr_bpm, z1_min, z2_min, z3_min, z4_min, z5_min, elevation_gain_ft, elevation_loss_ft
- workout_splits: workout_id, date, mile, split_pace_display, split_pace_min_mi, split_distance_mi, split_duration_min, elev_gain_ft, elev_loss_ft, avg_hr_bpm
- workout_hr_zones: activity_id, date, sport_type, z1_min, z2_min, z3_min, z4_min, z5_min, avg_hr_bpm, max_hr_bpm
- daily_health: date, active_calories_kcal, resting_hr_bpm, hrv_ms, steps, exercise_time_min
- daily_nutrition: date, calories_kcal, protein_g, carbs_g, fat_g
- daily_activity_summary: date, week_start, run_min, ride_min, strength_min, cardio_min, walk_min, z1_min, z2_min, z3_min, z4_min, z5_min, total_calories_kcal, steps
- personal_records: distance_label, sport, rank, best_time_sec, best_pace_display, achieved_date, distance_m
- body_composition: date, weight_lb, body_fat_pct, skeletal_muscle_mass_lb, inbody_score, visceral_fat_level
- performance_tests: id, test_type, value, unit, date, notes

POSTGRESQL DATE RULES:
- NEVER use YEAR(), MONTH(), DAY() — PostgreSQL only
- Current year: date >= '2026-01-01'
- Last 7 days: date >= CURRENT_DATE - INTERVAL '7 days'
- Month to date: date >= DATE_TRUNC('month', CURRENT_DATE)
- Always add LIMIT
"""

# ── Supabase helpers ──────────────────────────────────────────
def sb_headers():
    return {
        'apikey': SUPABASE_KEY,
        'Authorization': f'Bearer {SUPABASE_KEY}',
        'Content-Type': 'application/json'
    }

def run_query(sql):
    r = requests.post(
        f'{SUPABASE_URL}/rest/v1/rpc/run_query',
        headers=sb_headers(),
        json={'query_text': sql},
        timeout=15
    )
    r.raise_for_status()
    return r.json()

def get_current_phase():
    today = date.today().isoformat()
    for p in PHASES:
        if p['start'] <= today <= p['end']:
            return p
    return PHASES[-1]

def claude(system, user, max_tokens=1000):
    r = requests.post(
        'https://api.anthropic.com/v1/messages',
        headers={
            'Content-Type': 'application/json',
            'x-api-key': ANTHROPIC_KEY,
            'anthropic-version': '2023-06-01'
        },
        json={
            'model': 'claude-sonnet-4-20250514',
            'max_tokens': max_tokens,
            'system': system,
            'messages': [{'role': 'user', 'content': user}]
        },
        timeout=30
    )
    r.raise_for_status()
    return ''.join(b.get('text', '') for b in r.json().get('content', []))

# ── Auth ──────────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        if (request.form.get('username') == APP_USERNAME and
                request.form.get('password') == APP_PASSWORD):
            session['logged_in'] = True
            return redirect(url_for('index'))
        error = 'Invalid credentials'
    return render_template('login.html', error=error)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/')
@login_required
def index():
    return render_template('index.html')

# ── API: Dashboard ────────────────────────────────────────────
@app.route('/api/dashboard')
@login_required
def dashboard():
    try:
        body = run_query("""
            SELECT date, weight_lb, body_fat_pct, skeletal_muscle_mass_lb,
                   inbody_score, visceral_fat_level
            FROM body_composition ORDER BY date DESC LIMIT 1
        """)
        activity = run_query("""
            SELECT date, run_min, ride_min, strength_min, cardio_min, walk_min,
                   z1_min, z2_min, z3_min, z4_min, z5_min,
                   total_calories_kcal, steps
            FROM daily_activity_summary
            WHERE date >= CURRENT_DATE - INTERVAL '8 days'
            ORDER BY date
        """)
        health = run_query("""
            SELECT date, resting_hr_bpm, hrv_ms, steps, active_calories_kcal
            FROM daily_health
            WHERE date >= CURRENT_DATE - INTERVAL '7 days'
            ORDER BY date DESC LIMIT 7
        """)
        nutrition = run_query("""
            SELECT date, calories_kcal, protein_g, carbs_g, fat_g
            FROM daily_nutrition
            WHERE date >= CURRENT_DATE - INTERVAL '7 days'
            ORDER BY date DESC LIMIT 7
        """)
        workouts = run_query("""
            SELECT activity_id, date, sport_type, name, moving_time_min, distance_miles, avg_hr
            FROM workouts_strava
            WHERE date >= CURRENT_DATE - INTERVAL '8 days'
            ORDER BY date DESC
        """)

        nutrition_dates = {r['date'] for r in (nutrition or [])}
        missing_nutrition = []
        for i in range(7):
            d = (date.today() - timedelta(days=i+1)).isoformat()
            if d not in nutrition_dates:
                missing_nutrition.append(d)

        phase = get_current_phase()
        phase['days_remaining'] = (date.fromisoformat(phase['end']) - date.today()).days

        return jsonify({
            'body_comp':         body[0] if body else None,
            'activity':          activity or [],
            'health':            health or [],
            'nutrition':         nutrition or [],
            'workouts':          workouts or [],
            'missing_nutrition': missing_nutrition[:3],
            'phase':             phase
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ── API: Body comp history ────────────────────────────────────
@app.route('/api/body_comp_history')
@login_required
def body_comp_history():
    try:
        data = run_query("""
            SELECT date, weight_lb, body_fat_pct, skeletal_muscle_mass_lb,
                   inbody_score, visceral_fat_level
            FROM body_composition ORDER BY date
        """)
        return jsonify(data or [])
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ── API: Records ──────────────────────────────────────────────
@app.route('/api/records')
@login_required
def records():
    try:
        run_prs = run_query("""
            SELECT p.distance_label, p.rank, p.best_pace_display, p.best_time_sec,
                   p.achieved_date, s.activity_id
            FROM personal_records p
            LEFT JOIN workouts_strava s ON s.date = p.achieved_date
                AND s.sport_type = 'Run'
                AND ABS(s.distance_miles - (p.distance_m / 1609.34)) < 0.3
            WHERE p.sport = 'run' AND p.best_time_sec IS NOT NULL
            ORDER BY p.distance_m, p.rank
        """)
        ride_prs = run_query("""
            SELECT p.distance_label, p.rank, p.best_pace_display, p.best_time_sec,
                   p.achieved_date, s.activity_id
            FROM personal_records p
            LEFT JOIN workouts_strava s ON s.date = p.achieved_date
                AND s.sport_type IN ('Ride','GravelRide','VirtualRide')
                AND ABS(s.distance_miles - (p.distance_m / 1609.34)) < 0.5
            WHERE p.sport = 'ride' AND p.best_time_sec IS NOT NULL
            ORDER BY p.distance_m, p.rank
        """)

        return jsonify({'runs': run_prs or [], 'rides': ride_prs or []})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ── API: Performance tests ────────────────────────────────────
@app.route('/api/performance_tests')
@login_required
def get_performance_tests():
    try:
        data = run_query("""
            SELECT id, test_type, value, unit, date, notes
            FROM performance_tests
            ORDER BY test_type, date
        """)
        return jsonify(data or [])
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/performance_tests', methods=['POST'])
@login_required
def add_performance_test():
    try:
        data = request.json
        r = requests.post(
            f'{SUPABASE_URL}/rest/v1/performance_tests',
            headers={**sb_headers(), 'Prefer': 'return=representation'},
            json=[{
                'test_type': data.get('test_type'),
                'value':     float(data.get('value', 0)),
                'unit':      data.get('unit', ''),
                'date':      data.get('date', date.today().isoformat()),
                'notes':     data.get('notes', '')
            }]
        )
        r.raise_for_status()
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ── API: Chat ─────────────────────────────────────────────────
@app.route('/api/chat', methods=['POST'])
@login_required
def chat():
    data     = request.json
    question = data.get('question', '').strip()
    history  = data.get('history', [])
    if not question:
        return jsonify({'error': 'No question provided'}), 400

    try:
        q = question.lower()
        fetched = {}

        # ── Always fetch month-to-date for context ────────────
        fetched['month_to_date'] = run_query("""
            SELECT
                DATE_TRUNC('month', CURRENT_DATE)::date AS month_start,
                CURRENT_DATE AS through_date,
                COUNT(DISTINCT date) AS days_with_activity,
                ROUND(SUM(run_min)::numeric, 0) run_min,
                ROUND(SUM(ride_min)::numeric, 0) ride_min,
                ROUND(SUM(strength_min)::numeric, 0) strength_min,
                ROUND(SUM(cardio_min)::numeric, 0) cardio_min,
                ROUND(SUM(z2_min)::numeric, 0) z2_min,
                ROUND(SUM(z3_min+z4_min+z5_min)::numeric, 0) high_intensity_min,
                ROUND(SUM(total_calories_kcal)::numeric, 0) total_calories
            FROM daily_activity_summary
            WHERE date >= DATE_TRUNC('month', CURRENT_DATE)
        """)

        # ── Weekly/recent activity ────────────────────────────
        if any(w in q for w in ['last week', 'this week', 'training week', 'how was', 'weekly']):
            fetched['weekly_summary'] = run_query("""
                SELECT date, run_min, ride_min, strength_min, cardio_min, walk_min,
                       z1_min, z2_min, z3_min, z4_min, z5_min,
                       total_calories_kcal, steps
                FROM daily_activity_summary
                WHERE date >= CURRENT_DATE - INTERVAL '7 days'
                ORDER BY date
            """)
            fetched['workouts_week'] = run_query("""
                SELECT date, sport_type, name, moving_time_min,
                       distance_miles, avg_hr, calories
                FROM workouts_strava
                WHERE date >= CURRENT_DATE - INTERVAL '7 days'
                ORDER BY date
            """)

        # ── Nutrition ─────────────────────────────────────────
        if any(w in q for w in ['nutrition', 'food', 'eat', 'calorie', 'protein', 'macro', 'diet']):
            fetched['nutrition_recent'] = run_query("""
                SELECT date, calories_kcal, protein_g, carbs_g, fat_g
                FROM daily_nutrition
                WHERE date >= CURRENT_DATE - INTERVAL '30 days'
                ORDER BY date DESC LIMIT 30
            """)

        # ── Body composition ──────────────────────────────────
        if any(w in q for w in ['body', 'weight', 'fat', 'muscle', 'composition', 'inbody', 'scan']):
            fetched['body_comp'] = run_query("""
                SELECT date, weight_lb, body_fat_pct, skeletal_muscle_mass_lb,
                       inbody_score, visceral_fat_level
                FROM body_composition ORDER BY date
            """)

        # ── Cycling / rides ───────────────────────────────────
        if any(w in q for w in ['ride', 'cycling', 'bike', 'ms150', 'ms 150', 'longest']):
            fetched['rides'] = run_query("""
                SELECT date, sport_type, name, distance_miles, moving_time_min,
                       avg_hr, calories, total_elevation_gain_m
                FROM workouts_strava
                WHERE sport_type IN ('Ride','GravelRide','VirtualRide')
                AND date >= '2026-01-01'
                ORDER BY distance_miles DESC LIMIT 15
            """)

        # ── Running ───────────────────────────────────────────
        if any(w in q for w in ['run', 'pace', 'mile', 'marathon', '5k', '10k', 'half',
                                  'november', 'december', 'october', 'january', 'february',
                                  'september', 'august', 'hill', 'split', 'training block']):
            fetched['runs_strava'] = run_query("""
                SELECT date, name, sport_type, distance_miles,
                       moving_time_min, avg_hr, max_hr, total_elevation_gain_m
                FROM workouts_strava
                WHERE sport_type = 'Run'
                AND date >= CURRENT_DATE - INTERVAL '18 months'
                ORDER BY date DESC LIMIT 80
            """)
            fetched['runs_apple'] = run_query("""
                SELECT date, sport_type, distance_mi, avg_pace_display,
                       avg_hr_bpm, max_hr_bpm, elevation_gain_ft, elevation_loss_ft,
                       z1_min, z2_min, z3_min, z4_min, z5_min
                FROM workouts_apple
                WHERE sport_type ILIKE '%run%'
                AND date >= CURRENT_DATE - INTERVAL '18 months'
                ORDER BY date DESC LIMIT 80
            """)
            if any(w in q for w in ['hill', 'split', 'mile by mile', 'pace per mile',
                                      'january 11', 'half marathon', 'jan 11']):
                fetched['splits'] = run_query("""
                    SELECT s.date, s.mile, s.split_pace_display, s.split_pace_min_mi,
                           s.elev_gain_ft, s.elev_loss_ft, s.avg_hr_bpm
                    FROM workout_splits s
                    WHERE s.date >= CURRENT_DATE - INTERVAL '18 months'
                    ORDER BY s.date DESC, s.mile
                    LIMIT 300
                """)

        # ── PRs ───────────────────────────────────────────────
        if any(w in q for w in ['pr', 'personal record', 'best', 'fastest', 'record']):
            fetched['prs'] = run_query("""
                SELECT distance_label, sport, rank, best_pace_display,
                       best_time_sec, achieved_date
                FROM personal_records
                WHERE best_time_sec IS NOT NULL
                ORDER BY sport, distance_m, rank
            """)

        # ── Performance tests / KPIs ──────────────────────────
        if any(w in q for w in ['kpi', 'ftp', 'pull up', 'push up', 'pullup', 'pushup',
                                  'test', 'performance test', 'dip', 'plank']):
            fetched['performance_tests'] = run_query("""
                SELECT test_type, value, unit, date, notes
                FROM performance_tests
                ORDER BY test_type, date DESC
            """)

        # ── HR zones ─────────────────────────────────────────
        if any(w in q for w in ['zone', 'z2', 'heart rate', 'intensity', 'aerobic']):
            fetched['zone_trends'] = run_query("""
                SELECT DATE_TRUNC('month', date)::date as month,
                       ROUND(SUM(z1_min)::numeric, 0) z1,
                       ROUND(SUM(z2_min)::numeric, 0) z2,
                       ROUND(SUM(z3_min)::numeric, 0) z3,
                       ROUND(SUM(z4_min)::numeric, 0) z4,
                       ROUND(SUM(z5_min)::numeric, 0) z5
                FROM daily_activity_summary
                WHERE date >= CURRENT_DATE - INTERVAL '6 months'
                GROUP BY 1 ORDER BY 1
            """)

        # ── Overtraining / recovery ───────────────────────────
        if any(w in q for w in ['overtrain', 'recover', 'tired', 'hrv', 'resting hr',
                                  'fatigue', 'atl', 'ctl', 'tsb', 'training load',
                                  'fitness', 'form', 'fresh']):
            fetched['health_trend'] = run_query("""
                SELECT date, resting_hr_bpm, hrv_ms, steps, active_calories_kcal
                FROM daily_health
                WHERE date >= CURRENT_DATE - INTERVAL '21 days'
                ORDER BY date DESC
            """)
            fetched['weekly_load'] = run_query("""
                SELECT week_start,
                       ROUND(SUM(run_min)::numeric, 0) run_min,
                       ROUND(SUM(ride_min)::numeric, 0) ride_min,
                       ROUND(SUM(strength_min)::numeric, 0) strength_min,
                       ROUND(SUM(cardio_min)::numeric, 0) cardio_min,
                       ROUND(SUM(z2_min)::numeric, 0) z2_min,
                       ROUND(SUM(z3_min+z4_min+z5_min)::numeric, 0) high_intensity_min
                FROM daily_activity_summary
                WHERE date >= CURRENT_DATE - INTERVAL '8 weeks'
                GROUP BY week_start ORDER BY week_start
            """)

        # ── Recommendations / focus ───────────────────────────
        if any(w in q for w in ['focus', 'recommend', 'should i', 'what should', 'plan', 'this week']):
            fetched['weekly_load'] = run_query("""
                SELECT week_start,
                       ROUND(SUM(run_min)::numeric, 0) run_min,
                       ROUND(SUM(ride_min)::numeric, 0) ride_min,
                       ROUND(SUM(strength_min)::numeric, 0) strength_min,
                       ROUND(SUM(cardio_min)::numeric, 0) cardio_min
                FROM daily_activity_summary
                WHERE date >= CURRENT_DATE - INTERVAL '4 weeks'
                GROUP BY week_start ORDER BY week_start
            """)
            fetched['health_recent'] = run_query("""
                SELECT date, resting_hr_bpm, hrv_ms
                FROM daily_health
                WHERE date >= CURRENT_DATE - INTERVAL '7 days'
                ORDER BY date DESC
            """)

        # ── Month/time period questions ───────────────────────
        if any(w in q for w in ['this month', 'month', 'april', 'march', 'february',
                                  'january', 'year so far', 'this year']):
            fetched['monthly_breakdown'] = run_query("""
                SELECT DATE_TRUNC('month', date)::date as month,
                       ROUND(SUM(run_min)::numeric, 0) run_min,
                       ROUND(SUM(ride_min)::numeric, 0) ride_min,
                       ROUND(SUM(strength_min)::numeric, 0) strength_min,
                       ROUND(SUM(cardio_min)::numeric, 0) cardio_min,
                       ROUND(SUM(z2_min)::numeric, 0) z2_min,
                       COUNT(DISTINCT date) days_active
                FROM daily_activity_summary
                WHERE date >= '2026-01-01'
                GROUP BY 1 ORDER BY 1
            """)

        # ── Default fallback (always have something) ──────────
        if len(fetched) <= 1:  # only month_to_date
            fetched['weekly_summary'] = run_query("""
                SELECT date, run_min, ride_min, strength_min, cardio_min, walk_min,
                       z2_min, total_calories_kcal
                FROM daily_activity_summary
                WHERE date >= CURRENT_DATE - INTERVAL '7 days'
                ORDER BY date
            """)
            fetched['recent_workouts'] = run_query("""
                SELECT date, sport_type, name, moving_time_min, distance_miles, avg_hr
                FROM workouts_strava
                WHERE date >= CURRENT_DATE - INTERVAL '7 days'
                ORDER BY date DESC
            """)

        data_str = '\n\n'.join(
            f'## {k}\n{json.dumps(v, default=str)}'
            for k, v in fetched.items()
        )

        phase = get_current_phase()
        system = f"""{ATHLETE_CONTEXT}

CURRENT PHASE: {phase['name']}
Phase goal: {phase['goal']}
Focus metrics: {', '.join(phase['focus'])}
Watch for: {', '.join(phase['watch'])}

{DB_SCHEMA}

COACHING RULES:
- Answer using ONLY the data provided. Never invent numbers.
- Lead with the key insight. Be direct and specific.
- Always frame answers through the lens of the current phase goal.
- Flag concerns clearly: overtraining, under-eating, insufficient protein, too much high intensity.
- For body comp phase: prioritize body fat and muscle mass trends over performance metrics.
- For hill analysis: use elev_gain_ft and pace from workout_splits.
- month_to_date covers from the 1st of the current month to today ({TODAY}).
  If only a few days into the month, acknowledge that explicitly — don't extrapolate.
  Never use the 7-day weekly_summary to make monthly statements.
- Keep answers focused: 150-300 words unless a detailed plan is requested.
"""
        messages = []
        for h in history[-6:]:
            messages.append({'role': h['role'], 'content': h['content']})
        messages.append({'role': 'user', 'content': f'Question: {question}\n\nRelevant data:\n{data_str}'})

        r = requests.post(
            'https://api.anthropic.com/v1/messages',
            headers={'Content-Type': 'application/json', 'x-api-key': ANTHROPIC_KEY, 'anthropic-version': '2023-06-01'},
            json={'model': 'claude-sonnet-4-20250514', 'max_tokens': 1000, 'system': system, 'messages': messages},
            timeout=30
        )
        r.raise_for_status()
        answer = ''.join(b.get('text', '') for b in r.json().get('content', []))
        return jsonify({'answer': answer, 'queries_run': list(fetched.keys())})

    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ── API: Weekly report ────────────────────────────────────────
@app.route('/api/weekly_report')
@login_required
def weekly_report():
    try:
        activity = run_query("""
            SELECT week_start,
                   ROUND(SUM(run_min)::numeric, 0) run_min,
                   ROUND(SUM(ride_min)::numeric, 0) ride_min,
                   ROUND(SUM(strength_min)::numeric, 0) strength_min,
                   ROUND(SUM(cardio_min)::numeric, 0) cardio_min,
                   ROUND(SUM(z2_min)::numeric, 0) z2_min,
                   ROUND(SUM(z3_min+z4_min+z5_min)::numeric, 0) high_min
            FROM daily_activity_summary
            WHERE date >= CURRENT_DATE - INTERVAL '8 weeks'
            GROUP BY week_start ORDER BY week_start
        """)
        nutrition = run_query("""
            SELECT date, calories_kcal, protein_g
            FROM daily_nutrition
            WHERE date >= CURRENT_DATE - INTERVAL '7 days'
            ORDER BY date
        """)
        health = run_query("""
            SELECT date, resting_hr_bpm, hrv_ms
            FROM daily_health
            WHERE date >= CURRENT_DATE - INTERVAL '7 days'
            ORDER BY date
        """)
        body = run_query("""
            SELECT date, weight_lb, body_fat_pct, skeletal_muscle_mass_lb
            FROM body_composition ORDER BY date DESC LIMIT 2
        """)

        phase = get_current_phase()
        data_str = (
            f'## weekly_load\n{json.dumps(activity, default=str)}\n\n'
            f'## nutrition_week\n{json.dumps(nutrition, default=str)}\n\n'
            f'## health_week\n{json.dumps(health, default=str)}\n\n'
            f'## body_comp\n{json.dumps(body, default=str)}'
        )
        system = f"""{ATHLETE_CONTEXT}
CURRENT PHASE: {phase['name']}
Phase goal: {phase['goal']}

Generate a concise weekly training report:
1. Week summary (volume, intensity balance)
2. Body composition update
3. Nutrition check (calories, protein adequacy)
4. Recovery status (HRV, resting HR)
5. Top concern or highlight
6. 3 specific recommendations for next week

Be direct. Use actual numbers. Under 400 words."""

        report = claude(system, f'Generate weekly report.\n\nData:\n{data_str}', max_tokens=800)
        return jsonify({'report': report, 'phase': phase})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ── API: Zone goals ───────────────────────────────────────────
@app.route('/api/zone_goals')
@login_required
def zone_goals():
    try:
        recent_zones = run_query("""
            SELECT ROUND(SUM(z1_min)::numeric,0) total_z1, ROUND(SUM(z2_min)::numeric,0) total_z2,
                   ROUND(SUM(z3_min)::numeric,0) total_z3, ROUND(SUM(z4_min)::numeric,0) total_z4,
                   ROUND(SUM(z5_min)::numeric,0) total_z5
            FROM daily_activity_summary WHERE date >= CURRENT_DATE - INTERVAL '7 days'
        """)
        four_week = run_query("""
            SELECT week_start, ROUND(SUM(z1_min)::numeric,0) z1, ROUND(SUM(z2_min)::numeric,0) z2,
                   ROUND(SUM(z3_min)::numeric,0) z3, ROUND(SUM(z4_min)::numeric,0) z4,
                   ROUND(SUM(z5_min)::numeric,0) z5
            FROM daily_activity_summary WHERE date >= CURRENT_DATE - INTERVAL '4 weeks'
            GROUP BY week_start ORDER BY week_start
        """)
        phase = get_current_phase()
        prompt = f"""{ATHLETE_CONTEXT}
CURRENT PHASE: {phase['name']}
Phase goal: {phase['goal']}
Last 7 days: {json.dumps(recent_zones, default=str)}
Last 4 weeks: {json.dumps(four_week, default=str)}
Recommend weekly target minutes for each HR zone. Body comp phase: prioritize Z2, minimize Z4/Z5.
Respond with ONLY valid JSON: {{"z1":<min>,"z2":<min>,"z3":<min>,"z4":<min>,"z5":<min>,"rationale":"<one sentence>"}}"""
        text  = claude('', prompt, max_tokens=200)
        match = re.search(r'\{.*\}', text, re.DOTALL)
        if match:
            return jsonify(json.loads(match.group()))
        raise ValueError('No JSON')
    except Exception as e:
        return jsonify({'z1':60,'z2':180,'z3':45,'z4':20,'z5':10,
                       'rationale':'Default targets for body composition phase.'})

# ── API: Training load ────────────────────────────────────────
@app.route('/api/training_load')
@login_required
def training_load():
    try:
        activities = run_query("""
            SELECT date,
                   COALESCE(run_min,0)+COALESCE(ride_min,0)+COALESCE(strength_min,0)+
                   COALESCE(cardio_min,0)+COALESCE(walk_min,0) AS total_min,
                   COALESCE(z1_min,0) z1_min, COALESCE(z2_min,0) z2_min,
                   COALESCE(z3_min,0) z3_min, COALESCE(z4_min,0) z4_min,
                   COALESCE(z5_min,0) z5_min
            FROM daily_activity_summary
            WHERE date >= CURRENT_DATE - INTERVAL '90 days'
            ORDER BY date
        """)
        if not activities:
            return jsonify({'data': [], 'current': {}})

        IF = {'z1':0.55,'z2':0.72,'z3':0.87,'z4':0.98,'z5':1.10}
        def calc_tss(row):
            total = sum((row.get(f'{z}_min',0) or 0)/60*(f**2)*100 for z,f in IF.items())
            if total == 0 and row.get('total_min',0) > 0:
                total = (row['total_min']/60)*(0.65**2)*100
            return round(total, 1)

        tss_by_date = {r['date']: calc_tss(r) for r in activities}
        start = date.today() - timedelta(days=89)
        all_dates, d = [], start
        while d <= date.today():
            all_dates.append(d.isoformat())
            d += timedelta(days=1)

        atl = ctl = 0.0
        atl_decay, ctl_decay = 1-(1/7), 1-(1/42)
        results = []
        for d_str in all_dates:
            tss = tss_by_date.get(d_str, 0)
            atl = atl*atl_decay + tss*(1-atl_decay)
            ctl = ctl*ctl_decay + tss*(1-ctl_decay)
            results.append({'date':d_str,'tss':round(tss,1),'atl':round(atl,1),
                           'ctl':round(ctl,1),'tsb':round(ctl-atl,1)})

        return jsonify({'data': results[-60:], 'current': results[-1] if results else {}})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ── API: Workout detail ───────────────────────────────────────
@app.route('/api/workout/<activity_id>')
@login_required
def workout_detail(activity_id):
    try:
        activity = run_query(f"""
            SELECT activity_id, date, sport_type, name, distance_miles,
                   moving_time_min, avg_hr, max_hr, calories, total_elevation_gain_m
            FROM workouts_strava WHERE activity_id::text = '{str(activity_id)}' LIMIT 1
        """)
        if not activity:
            return jsonify({'error': 'Activity not found'}), 404
        act = activity[0]
        zones = run_query(f"""
            SELECT z1_min, z2_min, z3_min, z4_min, z5_min, avg_hr_bpm, max_hr_bpm
            FROM workout_hr_zones WHERE activity_id = {int(activity_id)} LIMIT 1
        """)
        splits, apple = [], []
        if act['sport_type'] == 'Run':
            apple = run_query(f"""
                SELECT workout_id, distance_mi, avg_pace_display, avg_pace_min_mi,
                       avg_hr_bpm, max_hr_bpm, elevation_gain_ft, elevation_loss_ft,
                       z1_min, z2_min, z3_min, z4_min, z5_min
                FROM workouts_apple
                WHERE date = '{act['date']}' AND sport_type ILIKE '%run%'
                ORDER BY ABS(distance_mi - {float(act['distance_miles'] or 0)}) LIMIT 1
            """)
            if apple:
                splits = run_query(f"""
                    SELECT mile, split_pace_display, split_pace_min_mi,
                           split_distance_mi, split_duration_min,
                           elev_gain_ft, elev_loss_ft, avg_hr_bpm
                    FROM workout_splits WHERE workout_id = '{apple[0]['workout_id']}' ORDER BY mile
                """)
        return jsonify({'activity':act,'zones':zones[0] if zones else None,
                       'splits':splits or [],'apple':apple[0] if apple else None})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ── API: Phases ───────────────────────────────────────────────
@app.route('/api/phases')
@login_required
def phases():
    today = date.today().isoformat()
    result = []
    for p in PHASES:
        ph = dict(p)
        ph['is_current']     = p['start'] <= today <= p['end']
        ph['days_remaining'] = (date.fromisoformat(p['end']) - date.today()).days
        result.append(ph)
    return jsonify(result)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5001))
    app.run(host='0.0.0.0', port=port, debug=False)
import os
import re
import json
import requests
from datetime import date, timedelta
from flask import Flask, request, jsonify, render_template, session, redirect, url_for
from functools import wraps

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'change-me-in-production')

# ── Config ────────────────────────────────────────────────────
SUPABASE_URL  = os.environ.get('SUPABASE_URL')
SUPABASE_KEY  = os.environ.get('SUPABASE_KEY')
ANTHROPIC_KEY = os.environ.get('ANTHROPIC_API_KEY')
APP_USERNAME  = os.environ.get('APP_USERNAME', 'admin')
APP_PASSWORD  = os.environ.get('APP_PASSWORD', 'changeme')

TODAY = date.today().isoformat()

# ── Training phases ───────────────────────────────────────────
PHASES = [
    {
        "name":  "Body Comp + MS 150",
        "start": "2026-01-01",
        "end":   "2026-04-27",
        "goal":  "Get body fat below 15% while maintaining/building muscle mass to 105-110 lbs. MS 150 bike ride April 25-26.",
        "focus": ["body_composition", "cycling", "nutrition", "z2_zones"],
        "watch": ["body_fat_pct", "muscle_mass", "calorie_balance", "protein_intake"],
        "color": "#c8a96e"
    },
    {
        "name":  "Recovery + Aerobic Base",
        "start": "2026-04-28",
        "end":   "2026-08-31",
        "goal":  "Post MS-150 recovery. Gradual running reintroduction. Continue body composition improvement. Build aerobic base with easy Z2 running.",
        "focus": ["body_composition", "nutrition", "easy_running", "z2_zones"],
        "watch": ["body_fat_pct", "muscle_mass", "run_volume_ramp", "hrv", "nutrition"],
        "color": "#4a7c59"
    },
    {
        "name":  "Houston Marathon Build",
        "start": "2026-09-01",
        "end":   "2027-01-17",
        "goal":  "Train for Houston Marathon (Jan 17, 2027). Target finish ~4:30, priority is finishing healthy. History of overtraining — keep increases conservative.",
        "focus": ["running", "long_runs", "nutrition_adequacy", "recovery"],
        "watch": ["weekly_mileage", "long_run_distance", "hrv", "resting_hr", "calorie_intake"],
        "color": "#4a6fa5"
    }
]

ATHLETE_CONTEXT = f"""
ATHLETE PROFILE:
- Age: 47, Male, drilling engineer on sabbatical
- Today: {TODAY}
- History: overtraining prone, previously did keto (under-eating risk), needs structured progression

BODY COMPOSITION BASELINE (2026-03-26):
- Weight: 224.9 lbs | Body fat: 19.6% | Muscle mass: 104.1 lbs | InBody score: 99 | Visceral fat: 9

GOALS:
- Phase 1 (now -> Apr 27): Body fat <15%, muscle 105-110 lbs, complete MS 150 bike ride Apr 25-26
- Phase 2 (Apr 28 -> Aug 31): Maintain body comp gains, easy running reintroduction, aerobic base
- Phase 3 (Sep 1 -> Jan 17): Houston Marathon, target ~4:30, priority = finish healthy

CURRENT PRs:
- 400m: 1:35 | 1 mile: 8:09 | 5K: 27:20 | 10K: 58:05 | Half Marathon: 2:06:06 (Jan 2026)

HR ZONES: Z1 <130 | Z2 131-150 | Z3 151-160 | Z4 161-170 | Z5 >171 bpm

KEY COACHING RULES:
- Never increase weekly run mileage more than 10% per week
- Flag if nutrition calories are too low for training load (muscle loss risk)
- Flag if protein looks insufficient on high training days
- Z2 training is the priority for both fat loss and aerobic base building
- Excessive Z4/Z5 during body comp phase risks muscle catabolism
- Watch HRV and resting HR trends for overtraining signs
- Nutrition tracking reminder: flag days with no nutrition log
- DATA WINDOWS: weekly_summary = last 7 days, month_to_date = from 1st of current month to today.
  NEVER say "this month" based only on 7-day data. Always use month_to_date for monthly statements.
  If the month just started and month_to_date only covers a few days, say so explicitly.
"""

DB_SCHEMA = """
DATABASE SCHEMA (PostgreSQL):
- workouts_strava: activity_id, date, sport_type, name, moving_time_min, distance_miles, avg_hr, max_hr, calories, total_elevation_gain_m. sport_type: Run/Ride/GravelRide/VirtualRide/Workout/Strength/Cardio/Walk
- workouts_apple: workout_id, date, sport_type, distance_mi, duration_min, avg_pace_display, avg_hr_bpm, max_hr_bpm, z1_min, z2_min, z3_min, z4_min, z5_min, elevation_gain_ft, elevation_loss_ft
- workout_splits: workout_id, date, mile, split_pace_display, split_pace_min_mi, split_distance_mi, split_duration_min, elev_gain_ft, elev_loss_ft, avg_hr_bpm
- workout_hr_zones: activity_id, date, sport_type, z1_min, z2_min, z3_min, z4_min, z5_min, avg_hr_bpm, max_hr_bpm
- daily_health: date, active_calories_kcal, resting_hr_bpm, hrv_ms, steps, exercise_time_min
- daily_nutrition: date, calories_kcal, protein_g, carbs_g, fat_g
- daily_activity_summary: date, week_start, run_min, ride_min, strength_min, cardio_min, walk_min, z1_min, z2_min, z3_min, z4_min, z5_min, total_calories_kcal, steps
- personal_records: distance_label, sport, rank, best_time_sec, best_pace_display, achieved_date, distance_m
- body_composition: date, weight_lb, body_fat_pct, skeletal_muscle_mass_lb, inbody_score, visceral_fat_level
- performance_tests: id, test_type, value, unit, date, notes

POSTGRESQL DATE RULES:
- NEVER use YEAR(), MONTH(), DAY() — PostgreSQL only
- Current year: date >= '2026-01-01'
- Last 7 days: date >= CURRENT_DATE - INTERVAL '7 days'
- Month to date: date >= DATE_TRUNC('month', CURRENT_DATE)
- Always add LIMIT
"""

# ── Supabase helpers ──────────────────────────────────────────
def sb_headers():
    return {
        'apikey': SUPABASE_KEY,
        'Authorization': f'Bearer {SUPABASE_KEY}',
        'Content-Type': 'application/json'
    }

def run_query(sql):
    r = requests.post(
        f'{SUPABASE_URL}/rest/v1/rpc/run_query',
        headers=sb_headers(),
        json={'query_text': sql},
        timeout=15
    )
    r.raise_for_status()
    return r.json()

def get_current_phase():
    today = date.today().isoformat()
    for p in PHASES:
        if p['start'] <= today <= p['end']:
            return p
    return PHASES[-1]

def claude(system, user, max_tokens=1000):
    r = requests.post(
        'https://api.anthropic.com/v1/messages',
        headers={
            'Content-Type': 'application/json',
            'x-api-key': ANTHROPIC_KEY,
            'anthropic-version': '2023-06-01'
        },
        json={
            'model': 'claude-sonnet-4-20250514',
            'max_tokens': max_tokens,
            'system': system,
            'messages': [{'role': 'user', 'content': user}]
        },
        timeout=30
    )
    r.raise_for_status()
    return ''.join(b.get('text', '') for b in r.json().get('content', []))

# ── Auth ──────────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        if (request.form.get('username') == APP_USERNAME and
                request.form.get('password') == APP_PASSWORD):
            session['logged_in'] = True
            return redirect(url_for('index'))
        error = 'Invalid credentials'
    return render_template('login.html', error=error)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/')
@login_required
def index():
    return render_template('index.html')

# ── API: Dashboard ────────────────────────────────────────────
@app.route('/api/dashboard')
@login_required
def dashboard():
    try:
        body = run_query("""
            SELECT date, weight_lb, body_fat_pct, skeletal_muscle_mass_lb,
                   inbody_score, visceral_fat_level
            FROM body_composition ORDER BY date DESC LIMIT 1
        """)
        activity = run_query("""
            SELECT date, run_min, ride_min, strength_min, cardio_min, walk_min,
                   z1_min, z2_min, z3_min, z4_min, z5_min,
                   total_calories_kcal, steps
            FROM daily_activity_summary
            WHERE date >= CURRENT_DATE - INTERVAL '8 days'
            ORDER BY date
        """)
        health = run_query("""
            SELECT date, resting_hr_bpm, hrv_ms, steps, active_calories_kcal
            FROM daily_health
            WHERE date >= CURRENT_DATE - INTERVAL '7 days'
            ORDER BY date DESC LIMIT 7
        """)
        nutrition = run_query("""
            SELECT date, calories_kcal, protein_g, carbs_g, fat_g
            FROM daily_nutrition
            WHERE date >= CURRENT_DATE - INTERVAL '7 days'
            ORDER BY date DESC LIMIT 7
        """)
        workouts = run_query("""
            SELECT activity_id, date, sport_type, name, moving_time_min, distance_miles, avg_hr
            FROM workouts_strava
            WHERE date >= CURRENT_DATE - INTERVAL '8 days'
            ORDER BY date DESC
        """)

        nutrition_dates = {r['date'] for r in (nutrition or [])}
        missing_nutrition = []
        for i in range(7):
            d = (date.today() - timedelta(days=i+1)).isoformat()
            if d not in nutrition_dates:
                missing_nutrition.append(d)

        phase = get_current_phase()
        phase['days_remaining'] = (date.fromisoformat(phase['end']) - date.today()).days

        return jsonify({
            'body_comp':         body[0] if body else None,
            'activity':          activity or [],
            'health':            health or [],
            'nutrition':         nutrition or [],
            'workouts':          workouts or [],
            'missing_nutrition': missing_nutrition[:3],
            'phase':             phase
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ── API: Body comp history ────────────────────────────────────
@app.route('/api/body_comp_history')
@login_required
def body_comp_history():
    try:
        data = run_query("""
            SELECT date, weight_lb, body_fat_pct, skeletal_muscle_mass_lb,
                   inbody_score, visceral_fat_level
            FROM body_composition ORDER BY date
        """)
        return jsonify(data or [])
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ── API: Records ──────────────────────────────────────────────
@app.route('/api/records')
@login_required
def records():
    try:
        run_prs = run_query("""
            SELECT distance_label, rank, best_pace_display, best_time_sec, achieved_date
            FROM personal_records
            WHERE sport = 'run' AND best_time_sec IS NOT NULL
            ORDER BY distance_m, rank
        """)
        ride_prs = run_query("""
            SELECT distance_label, rank, best_pace_display, best_time_sec, achieved_date
            FROM personal_records
            WHERE sport = 'ride' AND best_time_sec IS NOT NULL
            ORDER BY distance_m, rank
        """)
        return jsonify({'runs': run_prs or [], 'rides': ride_prs or []})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ── API: Performance tests ────────────────────────────────────
@app.route('/api/performance_tests')
@login_required
def get_performance_tests():
    try:
        data = run_query("""
            SELECT id, test_type, value, unit, date, notes
            FROM performance_tests
            ORDER BY test_type, date
        """)
        return jsonify(data or [])
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/performance_tests', methods=['POST'])
@login_required
def add_performance_test():
    try:
        data = request.json
        r = requests.post(
            f'{SUPABASE_URL}/rest/v1/performance_tests',
            headers={**sb_headers(), 'Prefer': 'return=representation'},
            json=[{
                'test_type': data.get('test_type'),
                'value':     float(data.get('value', 0)),
                'unit':      data.get('unit', ''),
                'date':      data.get('date', date.today().isoformat()),
                'notes':     data.get('notes', '')
            }]
        )
        r.raise_for_status()
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ── API: Chat ─────────────────────────────────────────────────
@app.route('/api/chat', methods=['POST'])
@login_required
def chat():
    data     = request.json
    question = data.get('question', '').strip()
    history  = data.get('history', [])
    if not question:
        return jsonify({'error': 'No question provided'}), 400

    try:
        q = question.lower()
        fetched = {}

        # ── Always fetch month-to-date for context ────────────
        fetched['month_to_date'] = run_query("""
            SELECT
                DATE_TRUNC('month', CURRENT_DATE)::date AS month_start,
                CURRENT_DATE AS through_date,
                COUNT(DISTINCT date) AS days_with_activity,
                ROUND(SUM(run_min)::numeric, 0) run_min,
                ROUND(SUM(ride_min)::numeric, 0) ride_min,
                ROUND(SUM(strength_min)::numeric, 0) strength_min,
                ROUND(SUM(cardio_min)::numeric, 0) cardio_min,
                ROUND(SUM(z2_min)::numeric, 0) z2_min,
                ROUND(SUM(z3_min+z4_min+z5_min)::numeric, 0) high_intensity_min,
                ROUND(SUM(total_calories_kcal)::numeric, 0) total_calories
            FROM daily_activity_summary
            WHERE date >= DATE_TRUNC('month', CURRENT_DATE)
        """)

        # ── Weekly/recent activity ────────────────────────────
        if any(w in q for w in ['last week', 'this week', 'training week', 'how was', 'weekly']):
            fetched['weekly_summary'] = run_query("""
                SELECT date, run_min, ride_min, strength_min, cardio_min, walk_min,
                       z1_min, z2_min, z3_min, z4_min, z5_min,
                       total_calories_kcal, steps
                FROM daily_activity_summary
                WHERE date >= CURRENT_DATE - INTERVAL '7 days'
                ORDER BY date
            """)
            fetched['workouts_week'] = run_query("""
                SELECT date, sport_type, name, moving_time_min,
                       distance_miles, avg_hr, calories
                FROM workouts_strava
                WHERE date >= CURRENT_DATE - INTERVAL '7 days'
                ORDER BY date
            """)

        # ── Nutrition ─────────────────────────────────────────
        if any(w in q for w in ['nutrition', 'food', 'eat', 'calorie', 'protein', 'macro', 'diet']):
            fetched['nutrition_recent'] = run_query("""
                SELECT date, calories_kcal, protein_g, carbs_g, fat_g
                FROM daily_nutrition
                WHERE date >= CURRENT_DATE - INTERVAL '30 days'
                ORDER BY date DESC LIMIT 30
            """)

        # ── Body composition ──────────────────────────────────
        if any(w in q for w in ['body', 'weight', 'fat', 'muscle', 'composition', 'inbody', 'scan']):
            fetched['body_comp'] = run_query("""
                SELECT date, weight_lb, body_fat_pct, skeletal_muscle_mass_lb,
                       inbody_score, visceral_fat_level
                FROM body_composition ORDER BY date
            """)

        # ── Cycling / rides ───────────────────────────────────
        if any(w in q for w in ['ride', 'cycling', 'bike', 'ms150', 'ms 150', 'longest']):
            fetched['rides'] = run_query("""
                SELECT date, sport_type, name, distance_miles, moving_time_min,
                       avg_hr, calories, total_elevation_gain_m
                FROM workouts_strava
                WHERE sport_type IN ('Ride','GravelRide','VirtualRide')
                AND date >= '2026-01-01'
                ORDER BY distance_miles DESC LIMIT 15
            """)

        # ── Running ───────────────────────────────────────────
        if any(w in q for w in ['run', 'pace', 'mile', 'marathon', '5k', '10k', 'half',
                                  'november', 'december', 'october', 'january', 'february',
                                  'september', 'august', 'hill', 'split', 'training block']):
            fetched['runs_strava'] = run_query("""
                SELECT date, name, sport_type, distance_miles,
                       moving_time_min, avg_hr, max_hr, total_elevation_gain_m
                FROM workouts_strava
                WHERE sport_type = 'Run'
                AND date >= CURRENT_DATE - INTERVAL '18 months'
                ORDER BY date DESC LIMIT 80
            """)
            fetched['runs_apple'] = run_query("""
                SELECT date, sport_type, distance_mi, avg_pace_display,
                       avg_hr_bpm, max_hr_bpm, elevation_gain_ft, elevation_loss_ft,
                       z1_min, z2_min, z3_min, z4_min, z5_min
                FROM workouts_apple
                WHERE sport_type ILIKE '%run%'
                AND date >= CURRENT_DATE - INTERVAL '18 months'
                ORDER BY date DESC LIMIT 80
            """)
            if any(w in q for w in ['hill', 'split', 'mile by mile', 'pace per mile',
                                      'january 11', 'half marathon', 'jan 11']):
                fetched['splits'] = run_query("""
                    SELECT s.date, s.mile, s.split_pace_display, s.split_pace_min_mi,
                           s.elev_gain_ft, s.elev_loss_ft, s.avg_hr_bpm
                    FROM workout_splits s
                    WHERE s.date >= CURRENT_DATE - INTERVAL '18 months'
                    ORDER BY s.date DESC, s.mile
                    LIMIT 300
                """)

        # ── PRs ───────────────────────────────────────────────
        if any(w in q for w in ['pr', 'personal record', 'best', 'fastest', 'record']):
            fetched['prs'] = run_query("""
                SELECT distance_label, sport, rank, best_pace_display,
                       best_time_sec, achieved_date
                FROM personal_records
                WHERE best_time_sec IS NOT NULL
                ORDER BY sport, distance_m, rank
            """)

        # ── Performance tests / KPIs ──────────────────────────
        if any(w in q for w in ['kpi', 'ftp', 'pull up', 'push up', 'pullup', 'pushup',
                                  'test', 'performance test', 'dip', 'plank']):
            fetched['performance_tests'] = run_query("""
                SELECT test_type, value, unit, date, notes
                FROM performance_tests
                ORDER BY test_type, date DESC
            """)

        # ── HR zones ─────────────────────────────────────────
        if any(w in q for w in ['zone', 'z2', 'heart rate', 'intensity', 'aerobic']):
            fetched['zone_trends'] = run_query("""
                SELECT DATE_TRUNC('month', date)::date as month,
                       ROUND(SUM(z1_min)::numeric, 0) z1,
                       ROUND(SUM(z2_min)::numeric, 0) z2,
                       ROUND(SUM(z3_min)::numeric, 0) z3,
                       ROUND(SUM(z4_min)::numeric, 0) z4,
                       ROUND(SUM(z5_min)::numeric, 0) z5
                FROM daily_activity_summary
                WHERE date >= CURRENT_DATE - INTERVAL '6 months'
                GROUP BY 1 ORDER BY 1
            """)

        # ── Overtraining / recovery ───────────────────────────
        if any(w in q for w in ['overtrain', 'recover', 'tired', 'hrv', 'resting hr',
                                  'fatigue', 'atl', 'ctl', 'tsb', 'training load',
                                  'fitness', 'form', 'fresh']):
            fetched['health_trend'] = run_query("""
                SELECT date, resting_hr_bpm, hrv_ms, steps, active_calories_kcal
                FROM daily_health
                WHERE date >= CURRENT_DATE - INTERVAL '21 days'
                ORDER BY date DESC
            """)
            fetched['weekly_load'] = run_query("""
                SELECT week_start,
                       ROUND(SUM(run_min)::numeric, 0) run_min,
                       ROUND(SUM(ride_min)::numeric, 0) ride_min,
                       ROUND(SUM(strength_min)::numeric, 0) strength_min,
                       ROUND(SUM(cardio_min)::numeric, 0) cardio_min,
                       ROUND(SUM(z2_min)::numeric, 0) z2_min,
                       ROUND(SUM(z3_min+z4_min+z5_min)::numeric, 0) high_intensity_min
                FROM daily_activity_summary
                WHERE date >= CURRENT_DATE - INTERVAL '8 weeks'
                GROUP BY week_start ORDER BY week_start
            """)

        # ── Recommendations / focus ───────────────────────────
        if any(w in q for w in ['focus', 'recommend', 'should i', 'what should', 'plan', 'this week']):
            fetched['weekly_load'] = run_query("""
                SELECT week_start,
                       ROUND(SUM(run_min)::numeric, 0) run_min,
                       ROUND(SUM(ride_min)::numeric, 0) ride_min,
                       ROUND(SUM(strength_min)::numeric, 0) strength_min,
                       ROUND(SUM(cardio_min)::numeric, 0) cardio_min
                FROM daily_activity_summary
                WHERE date >= CURRENT_DATE - INTERVAL '4 weeks'
                GROUP BY week_start ORDER BY week_start
            """)
            fetched['health_recent'] = run_query("""
                SELECT date, resting_hr_bpm, hrv_ms
                FROM daily_health
                WHERE date >= CURRENT_DATE - INTERVAL '7 days'
                ORDER BY date DESC
            """)

        # ── Month/time period questions ───────────────────────
        if any(w in q for w in ['this month', 'month', 'april', 'march', 'february',
                                  'january', 'year so far', 'this year']):
            fetched['monthly_breakdown'] = run_query("""
                SELECT DATE_TRUNC('month', date)::date as month,
                       ROUND(SUM(run_min)::numeric, 0) run_min,
                       ROUND(SUM(ride_min)::numeric, 0) ride_min,
                       ROUND(SUM(strength_min)::numeric, 0) strength_min,
                       ROUND(SUM(cardio_min)::numeric, 0) cardio_min,
                       ROUND(SUM(z2_min)::numeric, 0) z2_min,
                       COUNT(DISTINCT date) days_active
                FROM daily_activity_summary
                WHERE date >= '2026-01-01'
                GROUP BY 1 ORDER BY 1
            """)

        # ── Default fallback (always have something) ──────────
        if len(fetched) <= 1:  # only month_to_date
            fetched['weekly_summary'] = run_query("""
                SELECT date, run_min, ride_min, strength_min, cardio_min, walk_min,
                       z2_min, total_calories_kcal
                FROM daily_activity_summary
                WHERE date >= CURRENT_DATE - INTERVAL '7 days'
                ORDER BY date
            """)
            fetched['recent_workouts'] = run_query("""
                SELECT date, sport_type, name, moving_time_min, distance_miles, avg_hr
                FROM workouts_strava
                WHERE date >= CURRENT_DATE - INTERVAL '7 days'
                ORDER BY date DESC
            """)

        data_str = '\n\n'.join(
            f'## {k}\n{json.dumps(v, default=str)}'
            for k, v in fetched.items()
        )

        phase = get_current_phase()
        system = f"""{ATHLETE_CONTEXT}

CURRENT PHASE: {phase['name']}
Phase goal: {phase['goal']}
Focus metrics: {', '.join(phase['focus'])}
Watch for: {', '.join(phase['watch'])}

{DB_SCHEMA}

COACHING RULES:
- Answer using ONLY the data provided. Never invent numbers.
- Lead with the key insight. Be direct and specific.
- Always frame answers through the lens of the current phase goal.
- Flag concerns clearly: overtraining, under-eating, insufficient protein, too much high intensity.
- For body comp phase: prioritize body fat and muscle mass trends over performance metrics.
- For hill analysis: use elev_gain_ft and pace from workout_splits.
- month_to_date covers from the 1st of the current month to today ({TODAY}).
  If only a few days into the month, acknowledge that explicitly — don't extrapolate.
  Never use the 7-day weekly_summary to make monthly statements.
- Keep answers focused: 150-300 words unless a detailed plan is requested.
"""
        messages = []
        for h in history[-6:]:
            messages.append({'role': h['role'], 'content': h['content']})
        messages.append({'role': 'user', 'content': f'Question: {question}\n\nRelevant data:\n{data_str}'})

        r = requests.post(
            'https://api.anthropic.com/v1/messages',
            headers={'Content-Type': 'application/json', 'x-api-key': ANTHROPIC_KEY, 'anthropic-version': '2023-06-01'},
            json={'model': 'claude-sonnet-4-20250514', 'max_tokens': 1000, 'system': system, 'messages': messages},
            timeout=30
        )
        r.raise_for_status()
        answer = ''.join(b.get('text', '') for b in r.json().get('content', []))
        return jsonify({'answer': answer, 'queries_run': list(fetched.keys())})

    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ── API: Weekly report ────────────────────────────────────────
@app.route('/api/weekly_report')
@login_required
def weekly_report():
    try:
        activity = run_query("""
            SELECT week_start,
                   ROUND(SUM(run_min)::numeric, 0) run_min,
                   ROUND(SUM(ride_min)::numeric, 0) ride_min,
                   ROUND(SUM(strength_min)::numeric, 0) strength_min,
                   ROUND(SUM(cardio_min)::numeric, 0) cardio_min,
                   ROUND(SUM(z2_min)::numeric, 0) z2_min,
                   ROUND(SUM(z3_min+z4_min+z5_min)::numeric, 0) high_min
            FROM daily_activity_summary
            WHERE date >= CURRENT_DATE - INTERVAL '8 weeks'
            GROUP BY week_start ORDER BY week_start
        """)
        nutrition = run_query("""
            SELECT date, calories_kcal, protein_g
            FROM daily_nutrition
            WHERE date >= CURRENT_DATE - INTERVAL '7 days'
            ORDER BY date
        """)
        health = run_query("""
            SELECT date, resting_hr_bpm, hrv_ms
            FROM daily_health
            WHERE date >= CURRENT_DATE - INTERVAL '7 days'
            ORDER BY date
        """)
        body = run_query("""
            SELECT date, weight_lb, body_fat_pct, skeletal_muscle_mass_lb
            FROM body_composition ORDER BY date DESC LIMIT 2
        """)

        phase = get_current_phase()
        data_str = (
            f'## weekly_load\n{json.dumps(activity, default=str)}\n\n'
            f'## nutrition_week\n{json.dumps(nutrition, default=str)}\n\n'
            f'## health_week\n{json.dumps(health, default=str)}\n\n'
            f'## body_comp\n{json.dumps(body, default=str)}'
        )
        system = f"""{ATHLETE_CONTEXT}
CURRENT PHASE: {phase['name']}
Phase goal: {phase['goal']}

Generate a concise weekly training report:
1. Week summary (volume, intensity balance)
2. Body composition update
3. Nutrition check (calories, protein adequacy)
4. Recovery status (HRV, resting HR)
5. Top concern or highlight
6. 3 specific recommendations for next week

Be direct. Use actual numbers. Under 400 words."""

        report = claude(system, f'Generate weekly report.\n\nData:\n{data_str}', max_tokens=800)
        return jsonify({'report': report, 'phase': phase})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ── API: Zone goals ───────────────────────────────────────────
@app.route('/api/zone_goals')
@login_required
def zone_goals():
    try:
        recent_zones = run_query("""
            SELECT ROUND(SUM(z1_min)::numeric,0) total_z1, ROUND(SUM(z2_min)::numeric,0) total_z2,
                   ROUND(SUM(z3_min)::numeric,0) total_z3, ROUND(SUM(z4_min)::numeric,0) total_z4,
                   ROUND(SUM(z5_min)::numeric,0) total_z5
            FROM daily_activity_summary WHERE date >= CURRENT_DATE - INTERVAL '7 days'
        """)
        four_week = run_query("""
            SELECT week_start, ROUND(SUM(z1_min)::numeric,0) z1, ROUND(SUM(z2_min)::numeric,0) z2,
                   ROUND(SUM(z3_min)::numeric,0) z3, ROUND(SUM(z4_min)::numeric,0) z4,
                   ROUND(SUM(z5_min)::numeric,0) z5
            FROM daily_activity_summary WHERE date >= CURRENT_DATE - INTERVAL '4 weeks'
            GROUP BY week_start ORDER BY week_start
        """)
        phase = get_current_phase()
        prompt = f"""{ATHLETE_CONTEXT}
CURRENT PHASE: {phase['name']}
Phase goal: {phase['goal']}
Last 7 days: {json.dumps(recent_zones, default=str)}
Last 4 weeks: {json.dumps(four_week, default=str)}
Recommend weekly target minutes for each HR zone. Body comp phase: prioritize Z2, minimize Z4/Z5.
Respond with ONLY valid JSON: {{"z1":<min>,"z2":<min>,"z3":<min>,"z4":<min>,"z5":<min>,"rationale":"<one sentence>"}}"""
        text  = claude('', prompt, max_tokens=200)
        match = re.search(r'\{.*\}', text, re.DOTALL)
        if match:
            return jsonify(json.loads(match.group()))
        raise ValueError('No JSON')
    except Exception as e:
        return jsonify({'z1':60,'z2':180,'z3':45,'z4':20,'z5':10,
                       'rationale':'Default targets for body composition phase.'})

# ── API: Training load ────────────────────────────────────────
@app.route('/api/training_load')
@login_required
def training_load():
    try:
        activities = run_query("""
            SELECT date,
                   COALESCE(run_min,0)+COALESCE(ride_min,0)+COALESCE(strength_min,0)+
                   COALESCE(cardio_min,0)+COALESCE(walk_min,0) AS total_min,
                   COALESCE(z1_min,0) z1_min, COALESCE(z2_min,0) z2_min,
                   COALESCE(z3_min,0) z3_min, COALESCE(z4_min,0) z4_min,
                   COALESCE(z5_min,0) z5_min
            FROM daily_activity_summary
            WHERE date >= CURRENT_DATE - INTERVAL '90 days'
            ORDER BY date
        """)
        if not activities:
            return jsonify({'data': [], 'current': {}})

        IF = {'z1':0.55,'z2':0.72,'z3':0.87,'z4':0.98,'z5':1.10}
        def calc_tss(row):
            total = sum((row.get(f'{z}_min',0) or 0)/60*(f**2)*100 for z,f in IF.items())
            if total == 0 and row.get('total_min',0) > 0:
                total = (row['total_min']/60)*(0.65**2)*100
            return round(total, 1)

        tss_by_date = {r['date']: calc_tss(r) for r in activities}
        start = date.today() - timedelta(days=89)
        all_dates, d = [], start
        while d <= date.today():
            all_dates.append(d.isoformat())
            d += timedelta(days=1)

        atl = ctl = 0.0
        atl_decay, ctl_decay = 1-(1/7), 1-(1/42)
        results = []
        for d_str in all_dates:
            tss = tss_by_date.get(d_str, 0)
            atl = atl*atl_decay + tss*(1-atl_decay)
            ctl = ctl*ctl_decay + tss*(1-ctl_decay)
            results.append({'date':d_str,'tss':round(tss,1),'atl':round(atl,1),
                           'ctl':round(ctl,1),'tsb':round(ctl-atl,1)})

        return jsonify({'data': results[-60:], 'current': results[-1] if results else {}})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ── API: Workout detail ───────────────────────────────────────
@app.route('/api/workout/<activity_id>')
@login_required
def workout_detail(activity_id):
    try:
        activity = run_query(f"""
            SELECT activity_id, date, sport_type, name, distance_miles,
                   moving_time_min, avg_hr, max_hr, calories, total_elevation_gain_m
            FROM workouts_strava WHERE activity_id::text = '{str(activity_id)}' LIMIT 1
        """)
        if not activity:
            return jsonify({'error': 'Activity not found'}), 404
        act = activity[0]
        zones = run_query(f"""
            SELECT z1_min, z2_min, z3_min, z4_min, z5_min, avg_hr_bpm, max_hr_bpm
            FROM workout_hr_zones WHERE activity_id = {int(activity_id)} LIMIT 1
        """)
        splits, apple = [], []
        if act['sport_type'] == 'Run':
            apple = run_query(f"""
                SELECT workout_id, distance_mi, avg_pace_display, avg_pace_min_mi,
                       avg_hr_bpm, max_hr_bpm, elevation_gain_ft, elevation_loss_ft,
                       z1_min, z2_min, z3_min, z4_min, z5_min
                FROM workouts_apple
                WHERE date = '{act['date']}' AND sport_type ILIKE '%run%'
                ORDER BY ABS(distance_mi - {float(act['distance_miles'] or 0)}) LIMIT 1
            """)
            if apple:
                splits = run_query(f"""
                    SELECT mile, split_pace_display, split_pace_min_mi,
                           split_distance_mi, split_duration_min,
                           elev_gain_ft, elev_loss_ft, avg_hr_bpm
                    FROM workout_splits WHERE workout_id = '{apple[0]['workout_id']}' ORDER BY mile
                """)
        return jsonify({'activity':act,'zones':zones[0] if zones else None,
                       'splits':splits or [],'apple':apple[0] if apple else None})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ── API: Phases ───────────────────────────────────────────────
@app.route('/api/phases')
@login_required
def phases():
    today = date.today().isoformat()
    result = []
    for p in PHASES:
        ph = dict(p)
        ph['is_current']     = p['start'] <= today <= p['end']
        ph['days_remaining'] = (date.fromisoformat(p['end']) - date.today()).days
        result.append(ph)
    return jsonify(result)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5001))
    app.run(host='0.0.0.0', port=port, debug=False)
