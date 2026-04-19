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
- DATA WINDOWS: You have access to the full historical dataset — all workouts, nutrition, health metrics
  and body composition ever recorded. The data provided includes all_time_monthly (every month summarized),
  all_runs, all_rides (all historical), recent_workouts (last 30 days individual), and more.
  Use this full history to answer questions about any time period. Never say you can only see a limited window.
  month_to_date covers from the 1st of the current month to today ({TODAY}).
  If the month just started and month_to_date only covers a few days, say so explicitly.

CRITICAL DATE RULE:
- Today is {TODAY}. When referencing workouts, always use their actual date from the data.
- If a workout's date == today's date, call it "today's workout". NEVER call it "yesterday's workout".
- Only say "yesterday" if the workout date is literally yesterday's date.
"""

DB_SCHEMA = """
DATABASE SCHEMA (PostgreSQL):
- workouts_strava: activity_id, date, start_datetime, sport_type, name, moving_time_min, distance_miles, avg_hr, max_hr, calories, total_elevation_gain_m, avg_speed_mph. sport_type: Run/Ride/GravelRide/VirtualRide/Workout/Strength/Cardio/Walk
- workouts_apple: workout_id, date, sport_type, distance_mi, duration_min, avg_pace_display, avg_hr_bpm, max_hr_bpm, z1_min, z2_min, z3_min, z4_min, z5_min, elevation_gain_ft, elevation_loss_ft
- workout_splits: workout_id, date, mile, split_pace_display, split_pace_min_mi, split_distance_mi, split_duration_min, elev_gain_ft, elev_loss_ft, avg_hr_bpm
- workout_hr_zones: activity_id, date, sport_type, z1_min, z2_min, z3_min, z4_min, z5_min, avg_hr_bpm, max_hr_bpm
- daily_health: date, active_calories_kcal, resting_hr_bpm, hrv_ms, steps, exercise_time_min
- daily_nutrition: date, calories_kcal, protein_g, carbs_g, fat_g
- daily_activity_summary: date, week_start, run_min, ride_min, strength_min, cardio_min, walk_min, z1_min, z2_min, z3_min, z4_min, z5_min, total_calories_kcal, steps
- personal_records: distance_label, sport, rank, best_time_sec, best_pace_display, achieved_date, distance_m
- body_composition: date, weight_lb, body_fat_pct, skeletal_muscle_mass_lb, inbody_score, visceral_fat_level
- performance_tests: id, test_type, value, unit, date, notes
- goals: id, title, description, target_date, priority, status, impact_on_training, created_at

POSTGRESQL DATE RULES:
- NEVER use YEAR(), MONTH(), DAY() — PostgreSQL only
- Full history is available — data goes back to 2021
- Current year: date >= '2026-01-01'
- Month to date: date >= DATE_TRUNC('month', CURRENT_DATE)
- The data provided to you already spans the full history — use it all
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
            SELECT activity_id, date, start_datetime, sport_type,
                   name, moving_time_min, distance_miles, avg_hr
            FROM workouts_strava
            WHERE date >= CURRENT_DATE - INTERVAL '8 days'
            ORDER BY start_datetime DESC
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
                   p.achieved_date,
                   (SELECT s.activity_id FROM workouts_strava s
                    WHERE s.date = p.achieved_date AND s.sport_type = 'Run'
                    ORDER BY ABS(s.distance_miles - (p.distance_m / 1609.34)) LIMIT 1
                   ) AS activity_id
            FROM personal_records p
            WHERE p.sport = 'run' AND p.best_time_sec IS NOT NULL
            ORDER BY p.distance_m, p.rank
        """)
        ride_prs = run_query("""
            SELECT p.distance_label, p.rank, p.best_pace_display, p.best_time_sec,
                   p.achieved_date,
                   (SELECT s.activity_id FROM workouts_strava s
                    WHERE s.date = p.achieved_date
                    AND s.sport_type IN ('Ride','GravelRide','VirtualRide')
                    ORDER BY ABS(s.distance_miles - (p.distance_m / 1609.34)) LIMIT 1
                   ) AS activity_id
            FROM personal_records p
            WHERE p.sport = 'ride' AND p.best_time_sec IS NOT NULL
            ORDER BY p.distance_m, p.rank
        """)

        return jsonify({'runs': run_prs or [], 'rides': ride_prs or []})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ── API: Records — recalculate from raw workouts ──────────────
@app.route('/api/records/recalculate', methods=['POST'])
@login_required
def recalculate_records():
    """
    Recalculate all PRs from raw workouts_strava data.
    Uses DELETE+INSERT so no unique constraint is required.
    """
    try:
        ride_brackets = [
            (10,   16093.4,  '10 mi'),
            (20,   32186.9,  '20 mi'),
            (25,   40233.6,  '25 mi'),
            (30,   48280.3,  '30 mi'),
            (40,   64373.8,  '40 mi'),
            (50,   80467.2,  '50 mi'),
            (62.1, 99965.0,  '100K'),
            (100,  160934.0, '100 mi'),
        ]
        run_brackets = [
            (1,    1609.34,  '1 mi'),
            (3.1,  4989.0,   '5K'),
            (6.2,  9978.0,   '10K'),
            (13.1, 21082.0,  'Half Marathon'),
            (26.2, 42164.0,  'Marathon'),
        ]

        updated = []

        def recalc_sport(brackets, sport_filter_sql, sport_key):
            for dist_mi, dist_m, label in brackets:
                try:
                    rows = run_query(f"""
                        SELECT
                            date,
                            (moving_time_min * 60.0) / (distance_miles / {dist_mi}) AS estimated_time_sec,
                            distance_miles,
                            moving_time_min
                        FROM workouts_strava
                        WHERE sport_type IN ({sport_filter_sql})
                          AND distance_miles >= {dist_mi * 0.98}
                          AND moving_time_min IS NOT NULL
                          AND distance_miles IS NOT NULL
                        ORDER BY estimated_time_sec ASC
                        LIMIT 3
                    """)
                    if not rows:
                        continue

                    # Delete existing records for this sport+distance first
                    requests.delete(
                        f"{SUPABASE_URL}/rest/v1/personal_records"
                        f"?sport=eq.{sport_key}"
                        f"&distance_m=gte.{round(dist_m * 0.97, 1)}"
                        f"&distance_m=lte.{round(dist_m * 1.03, 1)}",
                        headers=sb_headers()
                    )

                    rows_to_insert = []
                    for rank_idx, r in enumerate(rows[:3], start=1):
                        t_sec = float(r['estimated_time_sec'])
                        if sport_key == 'ride':
                            speed_mph = dist_mi / (t_sec / 3600)
                            pace_d = f"{round(speed_mph, 1)} mph avg"
                        else:
                            p_sec = t_sec / dist_mi
                            pace_d = f"{int(p_sec // 60)}:{int(p_sec % 60):02d}/mi"

                        rows_to_insert.append({
                            'sport':             sport_key,
                            'distance_m':        round(dist_m, 1),
                            'distance_label':    label,
                            'rank':              rank_idx,
                            'best_time_sec':     round(t_sec, 1),
                            'best_pace_display': pace_d,
                            'achieved_date':     r['date']
                        })
                        if rank_idx == 1:
                            updated.append(f"{sport_key} {label}: {pace_d} on {r['date']}")

                    if rows_to_insert:
                        requests.post(
                            f"{SUPABASE_URL}/rest/v1/personal_records",
                            headers={**sb_headers(), 'Prefer': 'return=minimal'},
                            json=rows_to_insert
                        )

                except Exception as e:
                    print(f'Recalc error {sport_key} {label}: {e}')

        recalc_sport(ride_brackets, "'Ride','GravelRide','VirtualRide'", 'ride')
        recalc_sport(run_brackets,  "'Run'", 'run')

        return jsonify({'ok': True, 'updated': updated, 'count': len(updated)})
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

# ── API: Goals ────────────────────────────────────────────────
@app.route('/api/goals')
@login_required
def get_goals():
    try:
        data = run_query("""
            SELECT id, title, description, target_date, priority,
                   status, impact_on_training, created_at
            FROM goals
            ORDER BY target_date ASC NULLS LAST,
                     CASE priority WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END
        """)
        return jsonify(data or [])
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/goals', methods=['POST'])
@login_required
def add_goal():
    try:
        data = request.json
        r = requests.post(
            f'{SUPABASE_URL}/rest/v1/goals',
            headers={**sb_headers(), 'Prefer': 'return=representation'},
            json=[{
                'title':              data.get('title'),
                'description':        data.get('description', ''),
                'target_date':        data.get('target_date') or None,
                'priority':           data.get('priority', 'medium'),
                'status':             data.get('status', 'active'),
                'impact_on_training': data.get('impact_on_training', '')
            }]
        )
        r.raise_for_status()
        return jsonify(r.json()[0] if r.json() else {'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/goals/<int:goal_id>', methods=['PATCH'])
@login_required
def update_goal(goal_id):
    try:
        data    = request.json
        allowed = {'title', 'description', 'target_date', 'priority', 'status', 'impact_on_training'}
        payload = {k: v for k, v in data.items() if k in allowed}
        r = requests.patch(
            f'{SUPABASE_URL}/rest/v1/goals?id=eq.{goal_id}',
            headers=sb_headers(),
            json=payload
        )
        r.raise_for_status()
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/goals/<int:goal_id>', methods=['DELETE'])
@login_required
def delete_goal(goal_id):
    try:
        r = requests.delete(
            f'{SUPABASE_URL}/rest/v1/goals?id=eq.{goal_id}',
            headers=sb_headers()
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

        # ── Always fetch: month-to-date + full monthly breakdown + recent workouts ──
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

        # All-time monthly summary — always included for broad context
        fetched['all_time_monthly'] = run_query("""
            SELECT DATE_TRUNC('month', date)::date as month,
                   ROUND(SUM(run_min)::numeric, 0) run_min,
                   ROUND(SUM(ride_min)::numeric, 0) ride_min,
                   ROUND(SUM(strength_min)::numeric, 0) strength_min,
                   ROUND(SUM(cardio_min)::numeric, 0) cardio_min,
                   ROUND(SUM(z2_min)::numeric, 0) z2_min,
                   ROUND(SUM(z3_min+z4_min+z5_min)::numeric, 0) high_intensity_min,
                   COUNT(DISTINCT date) days_active
            FROM daily_activity_summary
            GROUP BY 1 ORDER BY 1
        """)

        # All workouts by date — always included
        fetched['all_workouts'] = run_query("""
            SELECT date, sport_type, name, moving_time_min, distance_miles, avg_hr, calories
            FROM workouts_strava
            ORDER BY date DESC LIMIT 500
        """)
        # Top workouts by distance — for longest/best questions
        fetched['top_runs_by_distance'] = run_query("""
            SELECT date, name, sport_type, distance_miles, moving_time_min, avg_hr
            FROM workouts_strava
            WHERE sport_type = 'Run' AND distance_miles IS NOT NULL
            ORDER BY distance_miles DESC LIMIT 10
        """)
        fetched['top_rides_by_distance'] = run_query("""
            SELECT date, name, sport_type, distance_miles, moving_time_min, avg_hr
            FROM workouts_strava
            WHERE sport_type IN ('Ride','GravelRide','VirtualRide') AND distance_miles IS NOT NULL
            ORDER BY distance_miles DESC LIMIT 10
        """)

        # Full health history — always included
        fetched['all_health'] = run_query("""
            SELECT date, resting_hr_bpm, hrv_ms, steps, active_calories_kcal
            FROM daily_health
            ORDER BY date DESC LIMIT 365
        """)

        # Full nutrition history — always included
        fetched['all_nutrition'] = run_query("""
            SELECT date, calories_kcal, protein_g, carbs_g, fat_g
            FROM daily_nutrition
            ORDER BY date DESC LIMIT 365
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

        # ── Nutrition ─────────────────────────────────────────
        if any(w in q for w in ['nutrition', 'food', 'eat', 'calorie', 'protein', 'macro', 'diet']):
            fetched['nutrition_all'] = run_query("""
                SELECT date, calories_kcal, protein_g, carbs_g, fat_g
                FROM daily_nutrition
                ORDER BY date DESC LIMIT 120
            """)
            fetched['nutrition_monthly_avg'] = run_query("""
                SELECT DATE_TRUNC('month', date)::date as month,
                       ROUND(AVG(calories_kcal)::numeric, 0) avg_calories,
                       ROUND(AVG(protein_g)::numeric, 1) avg_protein,
                       ROUND(AVG(carbs_g)::numeric, 1) avg_carbs,
                       ROUND(AVG(fat_g)::numeric, 1) avg_fat,
                       COUNT(*) days_logged
                FROM daily_nutrition
                GROUP BY 1 ORDER BY 1
            """)

        # ── Body composition ──────────────────────────────────
        if any(w in q for w in ['body', 'weight', 'fat', 'muscle', 'composition', 'inbody', 'scan']):
            fetched['body_comp'] = run_query("""
                SELECT date, weight_lb, body_fat_pct, skeletal_muscle_mass_lb,
                       inbody_score, visceral_fat_level
                FROM body_composition ORDER BY date
            """)

        # ── Cycling / rides ───────────────────────────────────
        if any(w in q for w in ['ride', 'cycling', 'bike', 'ms150', 'ms 150', 'longest', 'miles', 'furthest', 'farthest']):
            fetched['all_rides_by_date'] = run_query("""
                SELECT date, sport_type, name, distance_miles, moving_time_min,
                       avg_hr, calories, total_elevation_gain_m
                FROM workouts_strava
                WHERE sport_type IN ('Ride','GravelRide','VirtualRide')
                ORDER BY date DESC LIMIT 200
            """)
            fetched['all_rides_by_distance'] = run_query("""
                SELECT date, sport_type, name, distance_miles, moving_time_min,
                       avg_hr, calories, total_elevation_gain_m
                FROM workouts_strava
                WHERE sport_type IN ('Ride','GravelRide','VirtualRide')
                ORDER BY distance_miles DESC LIMIT 20
            """)

        # ── Running ───────────────────────────────────────────
        if any(w in q for w in ['run', 'pace', 'mile', 'marathon', '5k', '10k', 'half',
                                  'november', 'december', 'october', 'january', 'february',
                                  'september', 'august', 'hill', 'split', 'training block',
                                  'longest', 'furthest', 'farthest', 'best', 'fastest']):
            fetched['all_runs_by_date'] = run_query("""
                SELECT date, name, sport_type, distance_miles,
                       moving_time_min, avg_hr, max_hr, total_elevation_gain_m
                FROM workouts_strava
                WHERE sport_type = 'Run'
                ORDER BY date DESC LIMIT 300
            """)
            fetched['all_runs_by_distance'] = run_query("""
                SELECT date, name, sport_type, distance_miles,
                       moving_time_min, avg_hr, max_hr, total_elevation_gain_m
                FROM workouts_strava
                WHERE sport_type = 'Run'
                ORDER BY distance_miles DESC LIMIT 20
            """)
            fetched['runs_apple'] = run_query("""
                SELECT date, sport_type, distance_mi, avg_pace_display,
                       avg_hr_bpm, max_hr_bpm, elevation_gain_ft, elevation_loss_ft,
                       z1_min, z2_min, z3_min, z4_min, z5_min
                FROM workouts_apple
                WHERE sport_type ILIKE '%run%'
                ORDER BY date DESC LIMIT 300
            """)
            if any(w in q for w in ['hill', 'split', 'mile by mile', 'pace per mile',
                                      'january 11', 'half marathon', 'jan 11']):
                fetched['splits'] = run_query("""
                    SELECT s.date, s.mile, s.split_pace_display, s.split_pace_min_mi,
                           s.elev_gain_ft, s.elev_loss_ft, s.avg_hr_bpm
                    FROM workout_splits s
                    ORDER BY s.date DESC, s.mile
                    LIMIT 500
                """)

        # ── Strength / cardio workouts ────────────────────────
        if any(w in q for w in ['strength', 'lifting', 'weights', 'cardio', 'stair', 'workout']):
            fetched['strength_cardio'] = run_query("""
                SELECT date, sport_type, name, moving_time_min, avg_hr, calories
                FROM workouts_strava
                WHERE sport_type IN ('Strength','Workout','Cardio','StairStepper')
                ORDER BY date DESC LIMIT 100
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

        # ── Goals ─────────────────────────────────────────────
        if any(w in q for w in ['goal', 'race', 'event', '5k', 'priority', 'plan', 'upcoming']):
            fetched['goals'] = run_query("""
                SELECT title, description, target_date, priority, status, impact_on_training
                FROM goals
                WHERE status = 'active'
                ORDER BY target_date ASC NULLS LAST
            """)

        # ── Performance tests / KPIs ──────────────────────────
        if any(w in q for w in ['kpi', 'ftp', 'pull up', 'push up', 'pullup', 'pushup',
                                  'test', 'performance test', 'dip', 'plank', 'vo2']):
            fetched['performance_tests'] = run_query("""
                SELECT test_type, value, unit, date, notes
                FROM performance_tests
                ORDER BY test_type, date
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
                GROUP BY 1 ORDER BY 1
            """)

        # ── Overtraining / recovery ───────────────────────────
        if any(w in q for w in ['overtrain', 'recover', 'tired', 'hrv', 'resting hr',
                                  'fatigue', 'atl', 'ctl', 'tsb', 'training load',
                                  'fitness', 'form', 'fresh']):
            fetched['health_full'] = run_query("""
                SELECT date, resting_hr_bpm, hrv_ms, steps, active_calories_kcal
                FROM daily_health
                ORDER BY date DESC LIMIT 90
            """)
            fetched['weekly_load_all'] = run_query("""
                SELECT week_start,
                       ROUND(SUM(run_min)::numeric, 0) run_min,
                       ROUND(SUM(ride_min)::numeric, 0) ride_min,
                       ROUND(SUM(strength_min)::numeric, 0) strength_min,
                       ROUND(SUM(cardio_min)::numeric, 0) cardio_min,
                       ROUND(SUM(z2_min)::numeric, 0) z2_min,
                       ROUND(SUM(z3_min+z4_min+z5_min)::numeric, 0) high_intensity_min
                FROM daily_activity_summary
                GROUP BY week_start ORDER BY week_start
            """)

        # ── Recommendations / focus ───────────────────────────
        if any(w in q for w in ['focus', 'recommend', 'should i', 'what should', 'plan', 'this week']):
            fetched['weekly_load_recent'] = run_query("""
                SELECT week_start,
                       ROUND(SUM(run_min)::numeric, 0) run_min,
                       ROUND(SUM(ride_min)::numeric, 0) ride_min,
                       ROUND(SUM(strength_min)::numeric, 0) strength_min,
                       ROUND(SUM(cardio_min)::numeric, 0) cardio_min
                FROM daily_activity_summary
                WHERE date >= CURRENT_DATE - INTERVAL '8 weeks'
                GROUP BY week_start ORDER BY week_start
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
- You have FULL HISTORICAL DATA — all workouts, nutrition, health metrics ever recorded.
  Never tell the user you can only see a limited window. If a dataset key like all_time_monthly
  or all_runs is present, it covers the entire history. Use it.
- Lead with the key insight. Be direct and specific.
- Always frame answers through the lens of the current phase goal.
- Flag concerns clearly: overtraining, under-eating, insufficient protein, too much high intensity.
- For body comp phase: prioritize body fat and muscle mass trends over performance metrics.
- For hill analysis: use elev_gain_ft and pace from workout_splits.
- month_to_date covers from the 1st of the current month to today ({TODAY}).
  If only a few days into the month, acknowledge that explicitly — don't extrapolate.
- Keep answers focused: 150-300 words unless a detailed plan is requested.
- DATE RULE: If a workout's date == today ({TODAY}), refer to it as "today's workout", NEVER "yesterday's".
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
            SELECT activity_id, date, start_datetime, sport_type, name,
                   distance_miles, moving_time_min, avg_hr, max_hr,
                   calories, total_elevation_gain_m, avg_speed_mps
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

        # For runs: Apple Health splits
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

        # For rides: build speed/distance/elevation summary from stored columns
        speed_summary = None
        if act['sport_type'] in ('Ride', 'GravelRide', 'VirtualRide'):
            dist    = float(act.get('distance_miles') or 0)
            dur_min = float(act.get('moving_time_min') or 0)
            elev_m  = float(act.get('total_elevation_gain_m') or 0)
            # Convert avg_speed_mps (m/s) → mph; fall back to dist/time if missing
            mps     = float(act.get('avg_speed_mps') or 0)
            avg_sp  = round(mps * 2.23694, 1) if mps else (
                round(dist / (dur_min / 60), 1) if dur_min > 0 else 0
            )
            speed_summary = {
                'avg_speed_mph':     avg_sp,
                'distance_miles':    round(dist, 2),
                'duration_min':      round(dur_min, 1),
                'elevation_gain_ft': round(elev_m * 3.28084),
                'calories':          act.get('calories'),
                'avg_hr':            act.get('avg_hr'),
                'max_hr':            act.get('max_hr'),
            }

        return jsonify({
            'activity':      act,
            'zones':         zones[0] if zones else None,
            'splits':        splits or [],
            'apple':         apple[0] if apple else None,
            'speed_summary': speed_summary
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ── API: Nutrition goals ──────────────────────────────────────
@app.route('/api/nutrition_goals')
@login_required
def nutrition_goals():
    try:
        recent_nutrition = run_query("""
            SELECT date, calories_kcal, protein_g, carbs_g, fat_g
            FROM daily_nutrition
            WHERE date >= CURRENT_DATE - INTERVAL '7 days'
            ORDER BY date DESC
        """)
        recent_training = run_query("""
            SELECT date, run_min, ride_min, strength_min, cardio_min,
                   z2_min, z3_min, z4_min, z5_min
            FROM daily_activity_summary
            WHERE date >= CURRENT_DATE - INTERVAL '7 days'
            ORDER BY date DESC
        """)
        body_comp = run_query("""
            SELECT date, weight_lb, body_fat_pct, skeletal_muscle_mass_lb
            FROM body_composition ORDER BY date DESC LIMIT 1
        """)

        phase = get_current_phase()

        prompt = f"""{ATHLETE_CONTEXT}

CURRENT PHASE: {phase['name']}
Phase goal: {phase['goal']}

RECENT NUTRITION (last 7 days):
{json.dumps(recent_nutrition, default=str)}

RECENT TRAINING (last 7 days):
{json.dumps(recent_training, default=str)}

LATEST BODY COMP:
{json.dumps(body_comp, default=str)}

Based on the athlete's current phase, body composition goals, and recent training load,
recommend daily nutrition targets. Consider:
- Body comp phase: moderate calorie deficit to lose fat, high protein to preserve muscle
- Protein: 1g per lb of bodyweight minimum to preserve muscle during cut
- Carbs: enough to fuel workouts, limited on rest days
- Fat: healthy fats, not overly restricted
- Be realistic based on what they've actually been eating

Respond with ONLY a valid JSON object, no other text:
{{"calories": <kcal>, "protein_g": <g>, "carbs_g": <g>, "fat_g": <g>, "rationale": "<2 sentences explaining the targets>"}}"""

        text  = claude('', prompt, max_tokens=300)
        match = re.search(r'\{.*\}', text, re.DOTALL)
        if match:
            return jsonify(json.loads(match.group()))
        raise ValueError('No JSON in response')

    except Exception as e:
        return jsonify({
            'calories': 2200,
            'protein_g': 185,
            'carbs_g': 175,
            'fat_g': 65,
            'rationale': 'Default targets for body composition phase. High protein to preserve muscle, moderate deficit to lose fat.'
        })

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

# ── API: Strength logging ─────────────────────────────────────
@app.route('/api/exercises')
@login_required
def get_exercises():
    try:
        data = run_query("""
            SELECT id, name, category, muscle_group
            FROM exercises ORDER BY category, name
        """)
        return jsonify(data or [])
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/exercises', methods=['POST'])
@login_required
def add_exercise():
    try:
        data = request.json
        r = requests.post(
            f'{SUPABASE_URL}/rest/v1/exercises',
            headers={**sb_headers(), 'Prefer': 'return=representation'},
            json=[{
                'name':          data.get('name'),
                'category':      data.get('category', ''),
                'muscle_group':  data.get('muscle_group', ''),
                'exercise_type': data.get('exercise_type', 'weighted'),
                'workout_category': data.get('workout_category', 'Other')
            }]
        )
        r.raise_for_status()
        return jsonify(r.json()[0] if r.json() else {'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/strength/workout', methods=['POST'])
@login_required
def create_workout():
    try:
        data = request.json
        r = requests.post(
            f'{SUPABASE_URL}/rest/v1/strength_workouts',
            headers={**sb_headers(), 'Prefer': 'return=representation'},
            json=[{
                'date':  data.get('date', date.today().isoformat()),
                'notes': data.get('notes', '')
            }]
        )
        r.raise_for_status()
        return jsonify(r.json()[0])
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/strength/workout/<int:workout_id>/complete', methods=['POST'])
@login_required
def complete_workout(workout_id):
    try:
        r = requests.patch(
            f'{SUPABASE_URL}/rest/v1/strength_workouts?id=eq.{workout_id}',
            headers={**sb_headers(), 'Prefer': 'return=representation'},
            json={'completed_at': date.today().isoformat() + 'T00:00:00Z'}
        )
        r.raise_for_status()
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/strength/sets', methods=['POST'])
@login_required
def log_set():
    try:
        data = request.json
        r = requests.post(
            f'{SUPABASE_URL}/rest/v1/strength_sets',
            headers={**sb_headers(), 'Prefer': 'return=representation'},
            json=[{
                'workout_id':  data.get('workout_id'),
                'exercise':    data.get('exercise'),
                'set_number':  data.get('set_number'),
                'weight_lbs':  data.get('weight_lbs'),
                'reps':        data.get('reps'),
                'rpe':         data.get('rpe'),
                'notes':       data.get('notes', '')
            }]
        )
        r.raise_for_status()
        return jsonify(r.json()[0] if r.json() else {'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/strength/sets/<int:set_id>', methods=['DELETE'])
@login_required
def delete_set(set_id):
    try:
        r = requests.delete(
            f'{SUPABASE_URL}/rest/v1/strength_sets?id=eq.{set_id}',
            headers=sb_headers()
        )
        r.raise_for_status()
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/strength/history')
@login_required
def strength_history():
    try:
        workouts = run_query("""
            SELECT w.id, w.date, w.notes, w.completed_at,
                   COUNT(s.id) as set_count,
                   COUNT(DISTINCT s.exercise) as exercise_count
            FROM strength_workouts w
            LEFT JOIN strength_sets s ON s.workout_id = w.id
            GROUP BY w.id, w.date, w.notes, w.completed_at
            ORDER BY w.date DESC LIMIT 20
        """)
        return jsonify(workouts or [])
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/strength/workout/<int:workout_id>')
@login_required
def get_workout(workout_id):
    try:
        sets = run_query(f"""
            SELECT id, exercise, set_number, weight_lbs, reps, rpe, notes
            FROM strength_sets WHERE workout_id = {workout_id} ORDER BY exercise, set_number
        """)
        return jsonify(sets or [])
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/strength/workout/<int:workout_id>', methods=['DELETE'])
@login_required
def delete_workout(workout_id):
    try:
        requests.delete(
            f'{SUPABASE_URL}/rest/v1/strength_sets?workout_id=eq.{workout_id}',
            headers=sb_headers()
        )
        requests.delete(
            f'{SUPABASE_URL}/rest/v1/strength_workouts?id=eq.{workout_id}',
            headers=sb_headers()
        )
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ── API: Lift sessions (mobile workout logging) ───────────────
@app.route('/api/lift/session', methods=['POST'])
@login_required
def create_lift_session():
    try:
        data         = request.json
        workout_type = data.get('workout_type', 'General')
        session_date = data.get('date', date.today().isoformat())
        r = requests.post(
            f'{SUPABASE_URL}/rest/v1/workout_sessions',
            headers={**sb_headers(), 'Prefer': 'return=representation'},
            json=[{'workout_type': workout_type, 'date': session_date, 'status': 'active'}]
        )
        r.raise_for_status()
        session_id = r.json()[0]['id']
        url = f"https://web-production-fdff3.up.railway.app/lift/{session_id}"
        return jsonify({'session_id': session_id, 'url': url})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/lift/session/<session_id>')
def get_lift_session(session_id):
    try:
        sess = run_query(f"""
            SELECT id, date, workout_type, status
            FROM workout_sessions WHERE id = '{session_id}' LIMIT 1
        """)
        if not sess:
            return jsonify({'error': 'Session not found'}), 404

        sets = run_query(f"""
            SELECT s.id, s.exercise, s.section, s.set_number,
                   s.weight_lbs, s.reps, s.duration_sec, s.height_in, s.notes,
                   e.exercise_type, e.workout_category
            FROM strength_sets s
            LEFT JOIN exercises e ON e.name = s.exercise
            WHERE s.workout_id = (
                SELECT id FROM strength_workouts
                WHERE date = '{sess[0]['date']}'
                ORDER BY id DESC LIMIT 1
            )
            ORDER BY s.exercise, s.set_number
        """) or []

        exercises = run_query("""
            SELECT name, exercise_type, workout_category, muscle_group
            FROM exercises ORDER BY workout_category, name
        """) or []

        pbs = run_query("""
            SELECT s.exercise,
                   MAX(s.weight_lbs) as best_weight,
                   MAX(s.reps) as best_reps
            FROM strength_sets s
            GROUP BY s.exercise
        """) or []

        return jsonify({
            'session':   sess[0],
            'sets':      sets,
            'exercises': exercises,
            'pbs':       {p['exercise']: p for p in pbs}
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/lift/session/<session_id>/complete', methods=['POST'])
def complete_lift_session(session_id):
    try:
        sess = run_query(f"""
            SELECT id, date, workout_type FROM workout_sessions
            WHERE id = '{session_id}' LIMIT 1
        """)
        if not sess:
            return jsonify({'error': 'Session not found'}), 404
        requests.patch(
            f'{SUPABASE_URL}/rest/v1/workout_sessions?id=eq.{session_id}',
            headers=sb_headers(),
            json={'status': 'completed', 'completed_at': date.today().isoformat() + 'T00:00:00Z'}
        )
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/lift/session/<session_id>/delete', methods=['POST'])
def delete_lift_session(session_id):
    try:
        sess = run_query(f"""
            SELECT date FROM workout_sessions WHERE id = '{session_id}' LIMIT 1
        """)
        if sess:
            requests.delete(
                f'{SUPABASE_URL}/rest/v1/workout_sessions?id=eq.{session_id}',
                headers=sb_headers()
            )
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/lift/log', methods=['POST'])
def log_lift_set():
    try:
        data       = request.json
        session_id = data.get('session_id')

        sess = run_query(f"""
            SELECT date, workout_type FROM workout_sessions
            WHERE id = '{session_id}' LIMIT 1
        """)
        if not sess:
            return jsonify({'error': 'Invalid session'}), 404

        workout_date = sess[0]['date']
        workout_type = sess[0]['workout_type']

        existing = run_query(f"""
            SELECT id FROM strength_workouts
            WHERE date = '{workout_date}'
            ORDER BY id DESC LIMIT 1
        """)
        if existing:
            workout_id = existing[0]['id']
        else:
            r = requests.post(
                f'{SUPABASE_URL}/rest/v1/strength_workouts',
                headers={**sb_headers(), 'Prefer': 'return=representation'},
                json=[{'date': workout_date, 'notes': workout_type}]
            )
            r.raise_for_status()
            workout_id = r.json()[0]['id']

        row = {
            'workout_id':   workout_id,
            'exercise':     data.get('exercise'),
            'section':      data.get('section', 'Main Lifts'),
            'set_number':   data.get('set_number', 1),
            'weight_lbs':   data.get('weight_lbs'),
            'reps':         data.get('reps'),
            'duration_sec': data.get('duration_sec'),
            'height_in':    data.get('height_in'),
            'notes':        data.get('notes', '')
        }
        row = {k: v for k, v in row.items() if v is not None}
        r = requests.post(
            f'{SUPABASE_URL}/rest/v1/strength_sets',
            headers={**sb_headers(), 'Prefer': 'return=representation'},
            json=[row]
        )
        r.raise_for_status()
        return jsonify(r.json()[0] if r.json() else {'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/lift/set/<int:set_id>', methods=['DELETE'])
def delete_lift_set(set_id):
    try:
        requests.delete(
            f'{SUPABASE_URL}/rest/v1/strength_sets?id=eq.{set_id}',
            headers=sb_headers()
        )
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/lift/history')
@login_required
def lift_history():
    try:
        sessions = run_query("""
            SELECT ws.id, ws.date, ws.workout_type, ws.status,
                   COUNT(DISTINCT ss.exercise) as exercise_count,
                   COUNT(ss.id) as set_count
            FROM workout_sessions ws
            LEFT JOIN strength_workouts sw ON sw.date = ws.date
            LEFT JOIN strength_sets ss ON ss.workout_id = sw.id
            GROUP BY ws.id, ws.date, ws.workout_type, ws.status
            ORDER BY ws.date DESC LIMIT 30
        """)
        return jsonify(sessions or [])
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/exercises/all')
def get_all_exercises():
    """Public endpoint — no login — for mobile form."""
    try:
        data = run_query("""
            SELECT name, exercise_type, workout_category, muscle_group
            FROM exercises ORDER BY workout_category, name
        """)
        return jsonify(data or [])
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5001))
    app.run(host='0.0.0.0', port=port, debug=False)import os
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
- DATA WINDOWS: You have access to the full historical dataset — all workouts, nutrition, health metrics
  and body composition ever recorded. The data provided includes all_time_monthly (every month summarized),
  all_runs, all_rides (all historical), recent_workouts (last 30 days individual), and more.
  Use this full history to answer questions about any time period. Never say you can only see a limited window.
  month_to_date covers from the 1st of the current month to today ({TODAY}).
  If the month just started and month_to_date only covers a few days, say so explicitly.

CRITICAL DATE RULE:
- Today is {TODAY}. When referencing workouts, always use their actual date from the data.
- If a workout's date == today's date, call it "today's workout". NEVER call it "yesterday's workout".
- Only say "yesterday" if the workout date is literally yesterday's date.
"""

DB_SCHEMA = """
DATABASE SCHEMA (PostgreSQL):
- workouts_strava: activity_id, date, start_datetime, sport_type, name, moving_time_min, distance_miles, avg_hr, max_hr, calories, total_elevation_gain_m, avg_speed_mph. sport_type: Run/Ride/GravelRide/VirtualRide/Workout/Strength/Cardio/Walk
- workouts_apple: workout_id, date, sport_type, distance_mi, duration_min, avg_pace_display, avg_hr_bpm, max_hr_bpm, z1_min, z2_min, z3_min, z4_min, z5_min, elevation_gain_ft, elevation_loss_ft
- workout_splits: workout_id, date, mile, split_pace_display, split_pace_min_mi, split_distance_mi, split_duration_min, elev_gain_ft, elev_loss_ft, avg_hr_bpm
- workout_hr_zones: activity_id, date, sport_type, z1_min, z2_min, z3_min, z4_min, z5_min, avg_hr_bpm, max_hr_bpm
- daily_health: date, active_calories_kcal, resting_hr_bpm, hrv_ms, steps, exercise_time_min
- daily_nutrition: date, calories_kcal, protein_g, carbs_g, fat_g
- daily_activity_summary: date, week_start, run_min, ride_min, strength_min, cardio_min, walk_min, z1_min, z2_min, z3_min, z4_min, z5_min, total_calories_kcal, steps
- personal_records: distance_label, sport, rank, best_time_sec, best_pace_display, achieved_date, distance_m
- body_composition: date, weight_lb, body_fat_pct, skeletal_muscle_mass_lb, inbody_score, visceral_fat_level
- performance_tests: id, test_type, value, unit, date, notes
- goals: id, title, description, target_date, priority, status, impact_on_training, created_at

POSTGRESQL DATE RULES:
- NEVER use YEAR(), MONTH(), DAY() — PostgreSQL only
- Full history is available — data goes back to 2021
- Current year: date >= '2026-01-01'
- Month to date: date >= DATE_TRUNC('month', CURRENT_DATE)
- The data provided to you already spans the full history — use it all
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
            SELECT activity_id, date, sport_type,
                   name, moving_time_min, distance_miles, avg_hr
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
                   p.achieved_date,
                   (SELECT s.activity_id FROM workouts_strava s
                    WHERE s.date = p.achieved_date AND s.sport_type = 'Run'
                    ORDER BY ABS(s.distance_miles - (p.distance_m / 1609.34)) LIMIT 1
                   ) AS activity_id
            FROM personal_records p
            WHERE p.sport = 'run' AND p.best_time_sec IS NOT NULL
            ORDER BY p.distance_m, p.rank
        """)
        ride_prs = run_query("""
            SELECT p.distance_label, p.rank, p.best_pace_display, p.best_time_sec,
                   p.achieved_date,
                   (SELECT s.activity_id FROM workouts_strava s
                    WHERE s.date = p.achieved_date
                    AND s.sport_type IN ('Ride','GravelRide','VirtualRide')
                    ORDER BY ABS(s.distance_miles - (p.distance_m / 1609.34)) LIMIT 1
                   ) AS activity_id
            FROM personal_records p
            WHERE p.sport = 'ride' AND p.best_time_sec IS NOT NULL
            ORDER BY p.distance_m, p.rank
        """)

        return jsonify({'runs': run_prs or [], 'rides': ride_prs or []})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ── API: Records — recalculate from raw workouts ──────────────
@app.route('/api/records/recalculate', methods=['POST'])
@login_required
def recalculate_records():
    """
    Recalculate all PRs from raw workouts_strava data.
    Idempotent — safe to run any time. Replaces the manual DB script.
    """
    try:
        ride_brackets = [
            (10,   16093.4,  '10 mi'),
            (20,   32186.9,  '20 mi'),
            (25,   40233.6,  '25 mi'),
            (30,   48280.3,  '30 mi'),
            (40,   64373.8,  '40 mi'),
            (50,   80467.2,  '50 mi'),
            (62.1, 99965.0,  '100K'),
            (100,  160934.0, '100 mi'),
        ]
        run_brackets = [
            (1,    1609.34,  '1 mi'),
            (3.1,  4989.0,   '5K'),
            (6.2,  9978.0,   '10K'),
            (13.1, 21082.0,  'Half Marathon'),
            (26.2, 42164.0,  'Marathon'),
        ]

        updated = []

        def recalc_sport(brackets, sport_filter_sql, sport_key):
            for dist_mi, dist_m, label in brackets:
                try:
                    rows = run_query(f"""
                        SELECT
                            date,
                            (moving_time_min * 60.0) / (distance_miles / {dist_mi}) AS estimated_time_sec,
                            distance_miles,
                            moving_time_min
                        FROM workouts_strava
                        WHERE sport_type IN ({sport_filter_sql})
                          AND distance_miles >= {dist_mi * 0.98}
                          AND moving_time_min IS NOT NULL
                          AND distance_miles IS NOT NULL
                        ORDER BY estimated_time_sec ASC
                        LIMIT 3
                    """)
                    if not rows:
                        continue

                    for rank_idx, r in enumerate(rows[:3], start=1):
                        t_sec = float(r['estimated_time_sec'])
                        if sport_key == 'ride':
                            speed_mph = dist_mi / (t_sec / 3600)
                            pace_d = f"{round(speed_mph, 1)} mph avg"
                        else:
                            p_sec = t_sec / dist_mi
                            pace_d = f"{int(p_sec // 60)}:{int(p_sec % 60):02d}/mi"

                        requests.post(
                            f"{SUPABASE_URL}/rest/v1/personal_records?on_conflict=sport,distance_m,rank",
                            headers={**sb_headers(), 'Prefer': 'resolution=merge-duplicates'},
                            json=[{
                                'sport':             sport_key,
                                'distance_m':        round(dist_m, 1),
                                'distance_label':    label,
                                'rank':              rank_idx,
                                'best_time_sec':     round(t_sec, 1),
                                'best_pace_display': pace_d,
                                'achieved_date':     r['date']
                            }]
                        )
                        if rank_idx == 1:
                            updated.append(f"{sport_key} {label}: {pace_d} on {r['date']}")

                except Exception as e:
                    print(f'Recalc error {sport_key} {label}: {e}')

        recalc_sport(ride_brackets, "'Ride','GravelRide','VirtualRide'", 'ride')
        recalc_sport(run_brackets,  "'Run'", 'run')

        return jsonify({'ok': True, 'updated': updated, 'count': len(updated)})
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

# ── API: Goals ────────────────────────────────────────────────
@app.route('/api/goals')
@login_required
def get_goals():
    try:
        data = run_query("""
            SELECT id, title, description, target_date, priority,
                   status, impact_on_training, created_at
            FROM goals
            ORDER BY target_date ASC NULLS LAST,
                     CASE priority WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END
        """)
        return jsonify(data or [])
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/goals', methods=['POST'])
@login_required
def add_goal():
    try:
        data = request.json
        r = requests.post(
            f'{SUPABASE_URL}/rest/v1/goals',
            headers={**sb_headers(), 'Prefer': 'return=representation'},
            json=[{
                'title':              data.get('title'),
                'description':        data.get('description', ''),
                'target_date':        data.get('target_date') or None,
                'priority':           data.get('priority', 'medium'),
                'status':             data.get('status', 'active'),
                'impact_on_training': data.get('impact_on_training', '')
            }]
        )
        r.raise_for_status()
        return jsonify(r.json()[0] if r.json() else {'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/goals/<int:goal_id>', methods=['PATCH'])
@login_required
def update_goal(goal_id):
    try:
        data    = request.json
        allowed = {'title', 'description', 'target_date', 'priority', 'status', 'impact_on_training'}
        payload = {k: v for k, v in data.items() if k in allowed}
        r = requests.patch(
            f'{SUPABASE_URL}/rest/v1/goals?id=eq.{goal_id}',
            headers=sb_headers(),
            json=payload
        )
        r.raise_for_status()
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/goals/<int:goal_id>', methods=['DELETE'])
@login_required
def delete_goal(goal_id):
    try:
        r = requests.delete(
            f'{SUPABASE_URL}/rest/v1/goals?id=eq.{goal_id}',
            headers=sb_headers()
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

        # ── Always fetch: month-to-date + full monthly breakdown + recent workouts ──
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

        # All-time monthly summary — always included for broad context
        fetched['all_time_monthly'] = run_query("""
            SELECT DATE_TRUNC('month', date)::date as month,
                   ROUND(SUM(run_min)::numeric, 0) run_min,
                   ROUND(SUM(ride_min)::numeric, 0) ride_min,
                   ROUND(SUM(strength_min)::numeric, 0) strength_min,
                   ROUND(SUM(cardio_min)::numeric, 0) cardio_min,
                   ROUND(SUM(z2_min)::numeric, 0) z2_min,
                   ROUND(SUM(z3_min+z4_min+z5_min)::numeric, 0) high_intensity_min,
                   COUNT(DISTINCT date) days_active
            FROM daily_activity_summary
            GROUP BY 1 ORDER BY 1
        """)

        # All workouts by date — always included
        fetched['all_workouts'] = run_query("""
            SELECT date, sport_type, name, moving_time_min, distance_miles, avg_hr, calories
            FROM workouts_strava
            ORDER BY date DESC LIMIT 500
        """)
        # Top workouts by distance — for longest/best questions
        fetched['top_runs_by_distance'] = run_query("""
            SELECT date, name, sport_type, distance_miles, moving_time_min, avg_hr
            FROM workouts_strava
            WHERE sport_type = 'Run' AND distance_miles IS NOT NULL
            ORDER BY distance_miles DESC LIMIT 10
        """)
        fetched['top_rides_by_distance'] = run_query("""
            SELECT date, name, sport_type, distance_miles, moving_time_min, avg_hr
            FROM workouts_strava
            WHERE sport_type IN ('Ride','GravelRide','VirtualRide') AND distance_miles IS NOT NULL
            ORDER BY distance_miles DESC LIMIT 10
        """)

        # Full health history — always included
        fetched['all_health'] = run_query("""
            SELECT date, resting_hr_bpm, hrv_ms, steps, active_calories_kcal
            FROM daily_health
            ORDER BY date DESC LIMIT 365
        """)

        # Full nutrition history — always included
        fetched['all_nutrition'] = run_query("""
            SELECT date, calories_kcal, protein_g, carbs_g, fat_g
            FROM daily_nutrition
            ORDER BY date DESC LIMIT 365
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

        # ── Nutrition ─────────────────────────────────────────
        if any(w in q for w in ['nutrition', 'food', 'eat', 'calorie', 'protein', 'macro', 'diet']):
            fetched['nutrition_all'] = run_query("""
                SELECT date, calories_kcal, protein_g, carbs_g, fat_g
                FROM daily_nutrition
                ORDER BY date DESC LIMIT 120
            """)
            fetched['nutrition_monthly_avg'] = run_query("""
                SELECT DATE_TRUNC('month', date)::date as month,
                       ROUND(AVG(calories_kcal)::numeric, 0) avg_calories,
                       ROUND(AVG(protein_g)::numeric, 1) avg_protein,
                       ROUND(AVG(carbs_g)::numeric, 1) avg_carbs,
                       ROUND(AVG(fat_g)::numeric, 1) avg_fat,
                       COUNT(*) days_logged
                FROM daily_nutrition
                GROUP BY 1 ORDER BY 1
            """)

        # ── Body composition ──────────────────────────────────
        if any(w in q for w in ['body', 'weight', 'fat', 'muscle', 'composition', 'inbody', 'scan']):
            fetched['body_comp'] = run_query("""
                SELECT date, weight_lb, body_fat_pct, skeletal_muscle_mass_lb,
                       inbody_score, visceral_fat_level
                FROM body_composition ORDER BY date
            """)

        # ── Cycling / rides ───────────────────────────────────
        if any(w in q for w in ['ride', 'cycling', 'bike', 'ms150', 'ms 150', 'longest', 'miles', 'furthest', 'farthest']):
            fetched['all_rides_by_date'] = run_query("""
                SELECT date, sport_type, name, distance_miles, moving_time_min,
                       avg_hr, calories, total_elevation_gain_m
                FROM workouts_strava
                WHERE sport_type IN ('Ride','GravelRide','VirtualRide')
                ORDER BY date DESC LIMIT 200
            """)
            fetched['all_rides_by_distance'] = run_query("""
                SELECT date, sport_type, name, distance_miles, moving_time_min,
                       avg_hr, calories, total_elevation_gain_m
                FROM workouts_strava
                WHERE sport_type IN ('Ride','GravelRide','VirtualRide')
                ORDER BY distance_miles DESC LIMIT 20
            """)

        # ── Running ───────────────────────────────────────────
        if any(w in q for w in ['run', 'pace', 'mile', 'marathon', '5k', '10k', 'half',
                                  'november', 'december', 'october', 'january', 'february',
                                  'september', 'august', 'hill', 'split', 'training block',
                                  'longest', 'furthest', 'farthest', 'best', 'fastest']):
            fetched['all_runs_by_date'] = run_query("""
                SELECT date, name, sport_type, distance_miles,
                       moving_time_min, avg_hr, max_hr, total_elevation_gain_m
                FROM workouts_strava
                WHERE sport_type = 'Run'
                ORDER BY date DESC LIMIT 300
            """)
            fetched['all_runs_by_distance'] = run_query("""
                SELECT date, name, sport_type, distance_miles,
                       moving_time_min, avg_hr, max_hr, total_elevation_gain_m
                FROM workouts_strava
                WHERE sport_type = 'Run'
                ORDER BY distance_miles DESC LIMIT 20
            """)
            fetched['runs_apple'] = run_query("""
                SELECT date, sport_type, distance_mi, avg_pace_display,
                       avg_hr_bpm, max_hr_bpm, elevation_gain_ft, elevation_loss_ft,
                       z1_min, z2_min, z3_min, z4_min, z5_min
                FROM workouts_apple
                WHERE sport_type ILIKE '%run%'
                ORDER BY date DESC LIMIT 300
            """)
            if any(w in q for w in ['hill', 'split', 'mile by mile', 'pace per mile',
                                      'january 11', 'half marathon', 'jan 11']):
                fetched['splits'] = run_query("""
                    SELECT s.date, s.mile, s.split_pace_display, s.split_pace_min_mi,
                           s.elev_gain_ft, s.elev_loss_ft, s.avg_hr_bpm
                    FROM workout_splits s
                    ORDER BY s.date DESC, s.mile
                    LIMIT 500
                """)

        # ── Strength / cardio workouts ────────────────────────
        if any(w in q for w in ['strength', 'lifting', 'weights', 'cardio', 'stair', 'workout']):
            fetched['strength_cardio'] = run_query("""
                SELECT date, sport_type, name, moving_time_min, avg_hr, calories
                FROM workouts_strava
                WHERE sport_type IN ('Strength','Workout','Cardio','StairStepper')
                ORDER BY date DESC LIMIT 100
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

        # ── Goals ─────────────────────────────────────────────
        if any(w in q for w in ['goal', 'race', 'event', '5k', 'priority', 'plan', 'upcoming']):
            fetched['goals'] = run_query("""
                SELECT title, description, target_date, priority, status, impact_on_training
                FROM goals
                WHERE status = 'active'
                ORDER BY target_date ASC NULLS LAST
            """)

        # ── Performance tests / KPIs ──────────────────────────
        if any(w in q for w in ['kpi', 'ftp', 'pull up', 'push up', 'pullup', 'pushup',
                                  'test', 'performance test', 'dip', 'plank', 'vo2']):
            fetched['performance_tests'] = run_query("""
                SELECT test_type, value, unit, date, notes
                FROM performance_tests
                ORDER BY test_type, date
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
                GROUP BY 1 ORDER BY 1
            """)

        # ── Overtraining / recovery ───────────────────────────
        if any(w in q for w in ['overtrain', 'recover', 'tired', 'hrv', 'resting hr',
                                  'fatigue', 'atl', 'ctl', 'tsb', 'training load',
                                  'fitness', 'form', 'fresh']):
            fetched['health_full'] = run_query("""
                SELECT date, resting_hr_bpm, hrv_ms, steps, active_calories_kcal
                FROM daily_health
                ORDER BY date DESC LIMIT 90
            """)
            fetched['weekly_load_all'] = run_query("""
                SELECT week_start,
                       ROUND(SUM(run_min)::numeric, 0) run_min,
                       ROUND(SUM(ride_min)::numeric, 0) ride_min,
                       ROUND(SUM(strength_min)::numeric, 0) strength_min,
                       ROUND(SUM(cardio_min)::numeric, 0) cardio_min,
                       ROUND(SUM(z2_min)::numeric, 0) z2_min,
                       ROUND(SUM(z3_min+z4_min+z5_min)::numeric, 0) high_intensity_min
                FROM daily_activity_summary
                GROUP BY week_start ORDER BY week_start
            """)

        # ── Recommendations / focus ───────────────────────────
        if any(w in q for w in ['focus', 'recommend', 'should i', 'what should', 'plan', 'this week']):
            fetched['weekly_load_recent'] = run_query("""
                SELECT week_start,
                       ROUND(SUM(run_min)::numeric, 0) run_min,
                       ROUND(SUM(ride_min)::numeric, 0) ride_min,
                       ROUND(SUM(strength_min)::numeric, 0) strength_min,
                       ROUND(SUM(cardio_min)::numeric, 0) cardio_min
                FROM daily_activity_summary
                WHERE date >= CURRENT_DATE - INTERVAL '8 weeks'
                GROUP BY week_start ORDER BY week_start
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
- You have FULL HISTORICAL DATA — all workouts, nutrition, health metrics ever recorded.
  Never tell the user you can only see a limited window. If a dataset key like all_time_monthly
  or all_runs is present, it covers the entire history. Use it.
- Lead with the key insight. Be direct and specific.
- Always frame answers through the lens of the current phase goal.
- Flag concerns clearly: overtraining, under-eating, insufficient protein, too much high intensity.
- For body comp phase: prioritize body fat and muscle mass trends over performance metrics.
- For hill analysis: use elev_gain_ft and pace from workout_splits.
- month_to_date covers from the 1st of the current month to today ({TODAY}).
  If only a few days into the month, acknowledge that explicitly — don't extrapolate.
- Keep answers focused: 150-300 words unless a detailed plan is requested.
- DATE RULE: If a workout's date == today ({TODAY}), refer to it as "today's workout", NEVER "yesterday's".
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
        # Use only columns that are definitely stored in Supabase.
        # avg_speed_mps is the raw stored value; we convert to mph in Python.
        # start_datetime may not be synced to Supabase, so omit it here.
        activity = run_query(f"""
            SELECT activity_id, date, sport_type, name,
                   distance_miles, moving_time_min, avg_hr, max_hr,
                   calories, total_elevation_gain_m, avg_speed_mps
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

        # For runs: Apple Health splits
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

        # For rides: build speed/distance/elevation summary from stored columns
        speed_summary = None
        if act['sport_type'] in ('Ride', 'GravelRide', 'VirtualRide'):
            dist    = float(act.get('distance_miles') or 0)
            dur_min = float(act.get('moving_time_min') or 0)
            elev_m  = float(act.get('total_elevation_gain_m') or 0)
            # Convert avg_speed_mps (m/s) → mph; fall back to dist/time if missing
            mps     = float(act.get('avg_speed_mps') or 0)
            avg_sp  = round(mps * 2.23694, 1) if mps else (
                round(dist / (dur_min / 60), 1) if dur_min > 0 else 0
            )
            speed_summary = {
                'avg_speed_mph':     avg_sp,
                'distance_miles':    round(dist, 2),
                'duration_min':      round(dur_min, 1),
                'elevation_gain_ft': round(elev_m * 3.28084),
                'calories':          act.get('calories'),
                'avg_hr':            act.get('avg_hr'),
                'max_hr':            act.get('max_hr'),
            }

        return jsonify({
            'activity':      act,
            'zones':         zones[0] if zones else None,
            'splits':        splits or [],
            'apple':         apple[0] if apple else None,
            'speed_summary': speed_summary
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ── API: Nutrition goals ──────────────────────────────────────
@app.route('/api/nutrition_goals')
@login_required
def nutrition_goals():
    try:
        recent_nutrition = run_query("""
            SELECT date, calories_kcal, protein_g, carbs_g, fat_g
            FROM daily_nutrition
            WHERE date >= CURRENT_DATE - INTERVAL '7 days'
            ORDER BY date DESC
        """)
        recent_training = run_query("""
            SELECT date, run_min, ride_min, strength_min, cardio_min,
                   z2_min, z3_min, z4_min, z5_min
            FROM daily_activity_summary
            WHERE date >= CURRENT_DATE - INTERVAL '7 days'
            ORDER BY date DESC
        """)
        body_comp = run_query("""
            SELECT date, weight_lb, body_fat_pct, skeletal_muscle_mass_lb
            FROM body_composition ORDER BY date DESC LIMIT 1
        """)

        phase = get_current_phase()

        prompt = f"""{ATHLETE_CONTEXT}

CURRENT PHASE: {phase['name']}
Phase goal: {phase['goal']}

RECENT NUTRITION (last 7 days):
{json.dumps(recent_nutrition, default=str)}

RECENT TRAINING (last 7 days):
{json.dumps(recent_training, default=str)}

LATEST BODY COMP:
{json.dumps(body_comp, default=str)}

Based on the athlete's current phase, body composition goals, and recent training load,
recommend daily nutrition targets. Consider:
- Body comp phase: moderate calorie deficit to lose fat, high protein to preserve muscle
- Protein: 1g per lb of bodyweight minimum to preserve muscle during cut
- Carbs: enough to fuel workouts, limited on rest days
- Fat: healthy fats, not overly restricted
- Be realistic based on what they've actually been eating

Respond with ONLY a valid JSON object, no other text:
{{"calories": <kcal>, "protein_g": <g>, "carbs_g": <g>, "fat_g": <g>, "rationale": "<2 sentences explaining the targets>"}}"""

        text  = claude('', prompt, max_tokens=300)
        match = re.search(r'\{.*\}', text, re.DOTALL)
        if match:
            return jsonify(json.loads(match.group()))
        raise ValueError('No JSON in response')

    except Exception as e:
        return jsonify({
            'calories': 2200,
            'protein_g': 185,
            'carbs_g': 175,
            'fat_g': 65,
            'rationale': 'Default targets for body composition phase. High protein to preserve muscle, moderate deficit to lose fat.'
        })

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

# ── API: Strength logging ─────────────────────────────────────
@app.route('/api/exercises')
@login_required
def get_exercises():
    try:
        data = run_query("""
            SELECT id, name, category, muscle_group
            FROM exercises ORDER BY category, name
        """)
        return jsonify(data or [])
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/exercises', methods=['POST'])
@login_required
def add_exercise():
    try:
        data = request.json
        r = requests.post(
            f'{SUPABASE_URL}/rest/v1/exercises',
            headers={**sb_headers(), 'Prefer': 'return=representation'},
            json=[{
                'name':          data.get('name'),
                'category':      data.get('category', ''),
                'muscle_group':  data.get('muscle_group', ''),
                'exercise_type': data.get('exercise_type', 'weighted'),
                'workout_category': data.get('workout_category', 'Other')
            }]
        )
        r.raise_for_status()
        return jsonify(r.json()[0] if r.json() else {'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/strength/workout', methods=['POST'])
@login_required
def create_workout():
    try:
        data = request.json
        r = requests.post(
            f'{SUPABASE_URL}/rest/v1/strength_workouts',
            headers={**sb_headers(), 'Prefer': 'return=representation'},
            json=[{
                'date':  data.get('date', date.today().isoformat()),
                'notes': data.get('notes', '')
            }]
        )
        r.raise_for_status()
        return jsonify(r.json()[0])
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/strength/workout/<int:workout_id>/complete', methods=['POST'])
@login_required
def complete_workout(workout_id):
    try:
        r = requests.patch(
            f'{SUPABASE_URL}/rest/v1/strength_workouts?id=eq.{workout_id}',
            headers={**sb_headers(), 'Prefer': 'return=representation'},
            json={'completed_at': date.today().isoformat() + 'T00:00:00Z'}
        )
        r.raise_for_status()
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/strength/sets', methods=['POST'])
@login_required
def log_set():
    try:
        data = request.json
        r = requests.post(
            f'{SUPABASE_URL}/rest/v1/strength_sets',
            headers={**sb_headers(), 'Prefer': 'return=representation'},
            json=[{
                'workout_id':  data.get('workout_id'),
                'exercise':    data.get('exercise'),
                'set_number':  data.get('set_number'),
                'weight_lbs':  data.get('weight_lbs'),
                'reps':        data.get('reps'),
                'rpe':         data.get('rpe'),
                'notes':       data.get('notes', '')
            }]
        )
        r.raise_for_status()
        return jsonify(r.json()[0] if r.json() else {'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/strength/sets/<int:set_id>', methods=['DELETE'])
@login_required
def delete_set(set_id):
    try:
        r = requests.delete(
            f'{SUPABASE_URL}/rest/v1/strength_sets?id=eq.{set_id}',
            headers=sb_headers()
        )
        r.raise_for_status()
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/strength/history')
@login_required
def strength_history():
    try:
        workouts = run_query("""
            SELECT w.id, w.date, w.notes, w.completed_at,
                   COUNT(s.id) as set_count,
                   COUNT(DISTINCT s.exercise) as exercise_count
            FROM strength_workouts w
            LEFT JOIN strength_sets s ON s.workout_id = w.id
            GROUP BY w.id, w.date, w.notes, w.completed_at
            ORDER BY w.date DESC LIMIT 20
        """)
        return jsonify(workouts or [])
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/strength/workout/<int:workout_id>')
@login_required
def get_workout(workout_id):
    try:
        sets = run_query(f"""
            SELECT id, exercise, set_number, weight_lbs, reps, rpe, notes
            FROM strength_sets WHERE workout_id = {workout_id} ORDER BY exercise, set_number
        """)
        return jsonify(sets or [])
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/strength/workout/<int:workout_id>', methods=['DELETE'])
@login_required
def delete_workout(workout_id):
    try:
        requests.delete(
            f'{SUPABASE_URL}/rest/v1/strength_sets?workout_id=eq.{workout_id}',
            headers=sb_headers()
        )
        requests.delete(
            f'{SUPABASE_URL}/rest/v1/strength_workouts?id=eq.{workout_id}',
            headers=sb_headers()
        )
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ── API: Lift sessions (mobile workout logging) ───────────────
@app.route('/api/lift/session', methods=['POST'])
@login_required
def create_lift_session():
    try:
        data         = request.json
        workout_type = data.get('workout_type', 'General')
        session_date = data.get('date', date.today().isoformat())
        r = requests.post(
            f'{SUPABASE_URL}/rest/v1/workout_sessions',
            headers={**sb_headers(), 'Prefer': 'return=representation'},
            json=[{'workout_type': workout_type, 'date': session_date, 'status': 'active'}]
        )
        r.raise_for_status()
        session_id = r.json()[0]['id']
        url = f"https://web-production-fdff3.up.railway.app/lift/{session_id}"
        return jsonify({'session_id': session_id, 'url': url})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/lift/session/<session_id>')
def get_lift_session(session_id):
    try:
        sess = run_query(f"""
            SELECT id, date, workout_type, status
            FROM workout_sessions WHERE id = '{session_id}' LIMIT 1
        """)
        if not sess:
            return jsonify({'error': 'Session not found'}), 404

        sets = run_query(f"""
            SELECT s.id, s.exercise, s.section, s.set_number,
                   s.weight_lbs, s.reps, s.duration_sec, s.height_in, s.notes,
                   e.exercise_type, e.workout_category
            FROM strength_sets s
            LEFT JOIN exercises e ON e.name = s.exercise
            WHERE s.workout_id = (
                SELECT id FROM strength_workouts
                WHERE date = '{sess[0]['date']}'
                ORDER BY id DESC LIMIT 1
            )
            ORDER BY s.exercise, s.set_number
        """) or []

        exercises = run_query("""
            SELECT name, exercise_type, workout_category, muscle_group
            FROM exercises ORDER BY workout_category, name
        """) or []

        pbs = run_query("""
            SELECT s.exercise,
                   MAX(s.weight_lbs) as best_weight,
                   MAX(s.reps) as best_reps
            FROM strength_sets s
            GROUP BY s.exercise
        """) or []

        return jsonify({
            'session':   sess[0],
            'sets':      sets,
            'exercises': exercises,
            'pbs':       {p['exercise']: p for p in pbs}
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/lift/session/<session_id>/complete', methods=['POST'])
def complete_lift_session(session_id):
    try:
        sess = run_query(f"""
            SELECT id, date, workout_type FROM workout_sessions
            WHERE id = '{session_id}' LIMIT 1
        """)
        if not sess:
            return jsonify({'error': 'Session not found'}), 404
        requests.patch(
            f'{SUPABASE_URL}/rest/v1/workout_sessions?id=eq.{session_id}',
            headers=sb_headers(),
            json={'status': 'completed', 'completed_at': date.today().isoformat() + 'T00:00:00Z'}
        )
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/lift/session/<session_id>/delete', methods=['POST'])
def delete_lift_session(session_id):
    try:
        sess = run_query(f"""
            SELECT date FROM workout_sessions WHERE id = '{session_id}' LIMIT 1
        """)
        if sess:
            requests.delete(
                f'{SUPABASE_URL}/rest/v1/workout_sessions?id=eq.{session_id}',
                headers=sb_headers()
            )
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/lift/log', methods=['POST'])
def log_lift_set():
    try:
        data       = request.json
        session_id = data.get('session_id')

        sess = run_query(f"""
            SELECT date, workout_type FROM workout_sessions
            WHERE id = '{session_id}' LIMIT 1
        """)
        if not sess:
            return jsonify({'error': 'Invalid session'}), 404

        workout_date = sess[0]['date']
        workout_type = sess[0]['workout_type']

        existing = run_query(f"""
            SELECT id FROM strength_workouts
            WHERE date = '{workout_date}'
            ORDER BY id DESC LIMIT 1
        """)
        if existing:
            workout_id = existing[0]['id']
        else:
            r = requests.post(
                f'{SUPABASE_URL}/rest/v1/strength_workouts',
                headers={**sb_headers(), 'Prefer': 'return=representation'},
                json=[{'date': workout_date, 'notes': workout_type}]
            )
            r.raise_for_status()
            workout_id = r.json()[0]['id']

        row = {
            'workout_id':   workout_id,
            'exercise':     data.get('exercise'),
            'section':      data.get('section', 'Main Lifts'),
            'set_number':   data.get('set_number', 1),
            'weight_lbs':   data.get('weight_lbs'),
            'reps':         data.get('reps'),
            'duration_sec': data.get('duration_sec'),
            'height_in':    data.get('height_in'),
            'notes':        data.get('notes', '')
        }
        row = {k: v for k, v in row.items() if v is not None}
        r = requests.post(
            f'{SUPABASE_URL}/rest/v1/strength_sets',
            headers={**sb_headers(), 'Prefer': 'return=representation'},
            json=[row]
        )
        r.raise_for_status()
        return jsonify(r.json()[0] if r.json() else {'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/lift/set/<int:set_id>', methods=['DELETE'])
def delete_lift_set(set_id):
    try:
        requests.delete(
            f'{SUPABASE_URL}/rest/v1/strength_sets?id=eq.{set_id}',
            headers=sb_headers()
        )
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/lift/history')
@login_required
def lift_history():
    try:
        sessions = run_query("""
            SELECT ws.id, ws.date, ws.workout_type, ws.status,
                   COUNT(DISTINCT ss.exercise) as exercise_count,
                   COUNT(ss.id) as set_count
            FROM workout_sessions ws
            LEFT JOIN strength_workouts sw ON sw.date = ws.date
            LEFT JOIN strength_sets ss ON ss.workout_id = sw.id
            GROUP BY ws.id, ws.date, ws.workout_type, ws.status
            ORDER BY ws.date DESC LIMIT 30
        """)
        return jsonify(sessions or [])
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/exercises/all')
def get_all_exercises():
    """Public endpoint — no login — for mobile form."""
    try:
        data = run_query("""
            SELECT name, exercise_type, workout_category, muscle_group
            FROM exercises ORDER BY workout_category, name
        """)
        return jsonify(data or [])
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5001))
    app.run(host='0.0.0.0', port=port, debug=False)
