import streamlit as st
from ortools.sat.python import cp_model
from typing import Dict, List, Tuple, Any
import json
import pandas as pd

# Configure page
st.set_page_config(
    page_title="Time Table Generator",
    page_icon="📅",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Custom CSS for better styling
st.markdown("""
<style>
    .main-header {
        background: linear-gradient(90deg, #667eea 0%, #764ba2 100%);
        padding: 1rem;
        border-radius: 10px;
        color: white;
        text-align: center;
        margin-bottom: 2rem;
    }
    .constraint-box {
        background-color: #f0f2f6;
        padding: 1rem;
        border-radius: 5px;
        border-left: 5px solid #667eea;
        margin: 0.5rem 0;
    }
    .success-box {
        background-color: #d4edda;
        border: 1px solid #c3e6cb;
        color: #155724;
        padding: 1rem;
        border-radius: 5px;
        margin: 1rem 0;
    }
    .error-box {
        background-color: #f8d7da;
        border: 1px solid #f5c6cb;
        color: #721c24;
        padding: 1rem;
        border-radius: 5px;
        margin: 1rem 0;
    }
</style>
""", unsafe_allow_html=True)


def initialize_session_state():
    """Initialize session state variables"""
    if 'courses' not in st.session_state:
        st.session_state.courses = []
    if 'constraints' not in st.session_state:
        st.session_state.constraints = {}
    if 'generated_timetable' not in st.session_state:
        st.session_state.generated_timetable = None
    if 'last_error' not in st.session_state:
        st.session_state.last_error = None


def get_time_slots(slot_dict: Dict[str, int], start_times: Dict[str, int]) -> Tuple[List[str], Dict[int,int], Dict[str,str], Dict[str,int]]:
    """
    Generate time slots based on working days and hours.
    Returns:
      - slot_names: ordered list of slot ids (variable order used by solver)
      - slot_time: mapping slot_index -> start hour (int)
      - slot_to_day: mapping slot_id -> day in lowercase (e.g., 'monday')
      - day_slot_counts: mapping day.lower() -> number of slots for that day
    Notes:
      - Creates exactly `total_hours` slots per day.
      - Skips the lunch hour (12) when it would occur by moving to next hour.
    """
    slot_names: List[str] = []
    slot_time: Dict[int,int] = {}
    slot_to_day: Dict[str,str] = {}
    day_slot_counts: Dict[str,int] = {}

    day_abbreviations = {
        'Monday': 'M', 'Tuesday': 'T', 'Wednesday': 'W',
        'Thursday': 'Th', 'Friday': 'F', 'Saturday': 'Sa', 'Sunday': 'Su'
    }

    idx = 0
    for day, hours in slot_dict.items():
        hours = int(hours)
        start = int(start_times[day])
        abbrev = day_abbreviations.get(day, day[:2])
        day_count = 0

        for j in range(hours):  # create exactly `hours` slots
            # skip lunch hour if it would fall here
            while start == 12:
                start += 1
            slot_name = f"{abbrev}{j + 1}"
            slot_names.append(slot_name)
            slot_time[idx] = start
            slot_to_day[slot_name] = day.lower()
            day_count += 1
            idx += 1
            start += 1

        day_slot_counts[day.lower()] = day_count

    return slot_names, slot_time, slot_to_day, day_slot_counts


def generate_timetable_ortools(constraints: Dict[str, Any], courses: List[Dict[str, Any]], allow_free: bool = True, max_time_seconds: int = 10) -> Any:
    """
    Generate timetable using OR-Tools CP-SAT solver. Returns:
      - On success: dict mapping days (lowercase) to list of schedule entries.
      - On failure: dict with {'error': "message"}.
    Notes:
      - We create one interval per lecture occurrence (lectures_per_week occurrences, each of size `duration`).
      - We enforce teacher availability, day-boundary checks, no-overlap, consecutive/non-consecutive constraints (best-effort).
    """
    if not constraints or not courses:
        return {'error': "No constraints or courses provided."}

    working_days = constraints.get("working_days", [])
    if not working_days:
        return {'error': "No working days configured."}

    slot_counts_by_day = {}
    start_times = {}
    for d in working_days:
        day = d["day"]
        slot_counts_by_day[day] = int(d["total_hours"])
        start_times[day] = int(d["start_hr"])

    # Process courses
    subjects = []
    for course in courses:
        name = course["name"]
        lect_no = int(course['lectureno'])
        duration = int(course['duration'])
        subjects.append({
            'name': name,
            'lectures': lect_no,
            'duration': duration,
            'start_hr': int(course['start_hr']),
            'end_hr': int(course['end_hr'])
        })

    # Build time slots
    slot_names, slot_time, slot_to_day, day_slot_counts = get_time_slots(slot_counts_by_day, start_times)
    num_slots = len(slot_names)

    total_required_slots = sum(s['lectures'] * s['duration'] for s in subjects)
    total_available_slots = num_slots

    if total_available_slots < total_required_slots:
        return {'error': f"Total available slots ({total_available_slots}) < total required subject-slots ({total_required_slots}). Increase working hours or reduce lecture counts."}

    # We'll optionally add 'Free' pseudo-subject occurrences to fill remaining slots if allow_free
    free_subject_name = None
    free_occurrences = 0
    if total_available_slots > total_required_slots and allow_free:
        diff = total_available_slots - total_required_slots
        free_subject_name = "Free"
        cnt = 1
        existing_names = {s['name'] for s in subjects}
        while free_subject_name in existing_names:
            free_subject_name = f"Free_{cnt}"
            cnt += 1
        # Add as subject with `diff` occurrences of duration 1 and full availability
        subjects.append({'name': free_subject_name, 'lectures': diff, 'duration': 1, 'start_hr': 0, 'end_hr': 24})

    # Build CP-SAT model
    model = cp_model.CpModel()

    # We'll create for each lecture occurrence:
    # - start_var (int) -> domain = allowed starts (slot indices)
    # - end_var = start_var + duration
    # - interval = model.NewIntervalVar(start_var, duration, end_var)
    # Also we create indicator booleans start_at[(occ_id, s)] that indicate occurrence starts at slot s.

    occ_metadata = []  # list of dicts: {name, duration, start_var, interval, occ_id}
    start_at = {}  # (occ_id, s) -> BoolVar

    occ_id = 0
    name_to_occ_ids = {}

    # helper: prepare allowed starts for each subject occurrence
    allowed_starts_cache = {}

    for subj in subjects:
        name = subj['name']
        dur = subj['duration']
        allowed_vals = []
        for s in range(num_slots):
            # check day-boundary
            end_idx = s + dur - 1
            if end_idx >= num_slots:
                continue
            # ensure same day for whole duration
            if slot_to_day[slot_names[s]] != slot_to_day[slot_names[end_idx]]:
                continue
            # ensure all slots inside duration are within teacher availability
            ok = True
            for t in range(s, end_idx + 1):
                if slot_time[t] < subj['start_hr'] or slot_time[t] >= subj['end_hr']:
                    ok = False
                    break
            if ok:
                allowed_vals.append(s)
        allowed_starts_cache[name] = allowed_vals

    # If any subject has no allowed starts for any of its occurrences -> infeasible
    for subj in subjects:
        if not allowed_starts_cache[subj['name']] and subj['lectures'] > 0:
            return {'error': f"No feasible start slots for subject '{subj['name']}' given availability/day boundaries."}

    # Create occurrences
    for subj in subjects:
        name = subj['name']
        dur = subj['duration']
        allowed_vals = allowed_starts_cache[name]
        name_to_occ_ids.setdefault(name, [])
        for k in range(subj['lectures']):
            # domain from allowed_vals
            start_var = model.NewIntVarFromDomain(cp_model.Domain.FromValues(allowed_vals), f"{name}_s{occ_id}")
            end_var = model.NewIntVar(min(allowed_vals) + dur, max(allowed_vals) + dur, f"{name}_e{occ_id}")
            interval = model.NewIntervalVar(start_var, dur, end_var, f"{name}_it{occ_id}")
            occ_metadata.append({'occ_id': occ_id, 'name': name, 'duration': dur, 'start': start_var, 'interval': interval})
            name_to_occ_ids[name].append(occ_id)

            # create start_at booleans only for allowed starts
            for s in allowed_vals:
                b = model.NewBoolVar(f"occ{occ_id}_start_at_{s}")
                start_at[(occ_id, s)] = b
                # link boolean with start_var
                model.Add(start_var == s).OnlyEnforceIf(b)
                model.Add(start_var != s).OnlyEnforceIf(b.Not())

            occ_id += 1

    # No-overlap across all intervals -> single resource (same as your previous single timeline)
    all_intervals = [m['interval'] for m in occ_metadata]
    model.AddNoOverlap(all_intervals)

    # Consecutive / Non-consecutive handling (best-effort):
    cons = constraints.get('consecutive_subjects') or [""]
    noncons = constraints.get('non_consecutive_subjects') or [""]

    # Helper to add adjacency constraints: for each occurrence of A, require at least one occurrence of B adjacent
    def add_consecutive_pair(A: str, B: str):
        occs_A = name_to_occ_ids.get(A, [])
        occs_B = name_to_occ_ids.get(B, [])
        if not occs_A or not occs_B:
            return
        # For each occA, build adjacency booleans across occB and allowed slot positions
        for a in occs_A:
            adj_bools = []
            durA = next(m['duration'] for m in occ_metadata if m['occ_id'] == a)
            for b in occs_B:
                durB = next(m['duration'] for m in occ_metadata if m['occ_id'] == b)
                # adjacency can be: startA + durA == startB (B immediately after A)
                # or startB + durB == startA (A immediately after B)
                # we'll linearize using start_at booleans
                for s in allowed_starts_cache[A]:
                    s_after = s + durA
                    if s_after < num_slots and s_after in allowed_starts_cache[B]:
                        # create adj var that is 1 iff occ a starts at s AND occ b starts at s_after
                        adj = model.NewBoolVar(f"adj_a{a}_b{b}_s{s}")
                        model.AddBoolAnd([start_at[(a, s)], start_at[(b, s_after)]]).OnlyEnforceIf(adj)
                        # If adj is true, both start_at must be true. The reverse implication already holds from AddBoolAnd.
                        adj_bools.append(adj)
                for s in allowed_starts_cache[B]:
                    s_after = s + durB
                    if s_after < num_slots and s_after in allowed_starts_cache[A]:
                        # b at s and a at s_after -> adjacency in other direction
                        adj = model.NewBoolVar(f"adj_b{b}_a{a}_s{s}")
                        model.AddBoolAnd([start_at[(b, s)], start_at[(a, s_after)]]).OnlyEnforceIf(adj)
                        adj_bools.append(adj)
            if adj_bools:
                # require sum(adj_bools) >= 1
                model.Add(sum(adj_bools) >= 1)
            else:
                # No possible adjacency positions -> infeasible for this pair
                model.AddFalseConstraint()

    def add_non_consecutive_pair(A: str, B: str):
        occs_A = name_to_occ_ids.get(A, [])
        occs_B = name_to_occ_ids.get(B, [])
        if not occs_A or not occs_B:
            return
        for a in occs_A:
            for b in occs_B:
                durA = next(m['duration'] for m in occ_metadata if m['occ_id'] == a)
                durB = next(m['duration'] for m in occ_metadata if m['occ_id'] == b)
                # For every possible slot s for a where b could be adjacent after
                for s in allowed_starts_cache[A]:
                    s_after = s + durA
                    if s_after < num_slots and s_after in allowed_starts_cache[B]:
                        # cannot have both start_at[a,s] and start_at[b,s_after]
                        model.Add(start_at[(a, s)] + start_at[(b, s_after)] <= 1)
                # And the other direction
                for s in allowed_starts_cache[B]:
                    s_after = s + durB
                    if s_after < num_slots and s_after in allowed_starts_cache[A]:
                        model.Add(start_at[(b, s)] + start_at[(a, s_after)] <= 1)

    # Apply user-specified pairs (your UI currently supports single pair each)
    if cons and cons[0]:
        if len(cons) >= 2:
            add_consecutive_pair(cons[0], cons[1])
    if noncons and noncons[0]:
        if len(noncons) >= 2:
            add_non_consecutive_pair(noncons[0], noncons[1])

    # Solve
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = max_time_seconds
    solver.parameters.num_search_workers = 8

    result = solver.Solve(model)
    if result not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return {'error': "No valid timetable found with the given constraints. Try relaxing constraints or double-check availability/hours."}

    # Build response dict
    resp_data = {d.lower(): [] for d in ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]}

    for mdata in occ_metadata:
        occ_id = mdata['occ_id']
        name = mdata['name']
        dur = mdata['duration']
        start_idx = solver.Value(mdata['start'])
        assigned_slots = list(range(start_idx, start_idx + dur))
        # for each slot index, build entry with slot name and start/end
        slot_name = slot_names[start_idx]
        day = slot_to_day[slot_name]
        start_hr = slot_time[start_idx]
        end_hr = start_hr + 1
        # If duration >1, compute end_hr accordingly
        end_hr = start_hr + dur
        resp_data[day].append({
            'slot': slot_name,
            'subject': name,
            'start_time': f"{start_hr:02d}:00",
            'end_time': f"{end_hr:02d}:00"
        })

    # Sort each day by start_time
    for day_key in resp_data:
        resp_data[day_key].sort(key=lambda x: x['start_time'])

    return resp_data


# ----------------- Streamlit UI (kept mostly identical) -----------------

def main():
    initialize_session_state()

    # Header
    st.markdown("""
    <div class="main-header">
        <h1>📅 Time Table Generator</h1>
        <p>Dynamic scheduling system using OR-Tools CP-SAT</p>
    </div>
    """, unsafe_allow_html=True)

    # Sidebar for navigation
    st.sidebar.title("Navigation")
    tab = st.sidebar.radio("Select Option",
                          ["Add Courses", "Set Constraints", "Generate Timetable", "View Results"]) 

    if tab == "Add Courses":
        st.header("📚 Add Courses")

        with st.form("course_form"):
            col1, col2 = st.columns(2)

            with col1:
                course_name = st.text_input("Course Name", placeholder="e.g., Computer Networks")
                instructor_name = st.text_input("Instructor Name", placeholder="e.g., Dr. Smith")
                lectures_per_week = st.number_input("Lectures per Week", min_value=1, max_value=10, value=2)

            with col2:
                duration = st.selectbox("Duration per Lecture (hours)", [1, 2, 3], index=0)
                start_hr = st.number_input("Instructor Start Hour", min_value=6, max_value=20, value=9)
                end_hr = st.number_input("Instructor End Hour", min_value=7, max_value=22, value=17)

            submitted = st.form_submit_button("Add Course")

            if submitted:
                if course_name and instructor_name:
                    course = {
                        "name": course_name.strip(),
                        "instructor_name": instructor_name.strip(),
                        "lectureno": int(lectures_per_week),
                        "duration": int(duration),
                        "start_hr": str(int(start_hr)),
                        "end_hr": str(int(end_hr))
                    }
                    st.session_state.courses.append(course)
                    st.success(f"✅ Course '{course_name}' added successfully!")
                else:
                    st.error("❌ Please fill in all required fields.")

        # Display added courses
        if st.session_state.courses:
            st.subheader("Added Courses")
            for i, course in enumerate(st.session_state.courses, 1):
                st.write(f"**{i}. {course['name']}**")
                col1, col2, col3, col4 = st.columns(4)
                with col1:
                    st.write(f"Instructor: {course['instructor_name']}")
                with col2:
                    st.write(f"Lectures: {course['lectureno']}/week")
                with col3:
                    st.write(f"Duration: {course['duration']}h")
                with col4:
                    st.write(f"Available: {course['start_hr']}:00-{course['end_hr']}:00")
                st.divider()

            if st.button("Clear All Courses"):
                st.session_state.courses = []
                st.session_state.generated_timetable = None
                st.rerun()

    elif tab == "Set Constraints":
        st.header("⚙️ Set Constraints")

        # Working Days Configuration
        st.subheader("Working Days Configuration")
        working_days = []

        days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

        for day in days:
            col1, col2, col3, col4 = st.columns(4)

            with col1:
                include_day = st.checkbox(f"Include {day}", key=f"include_{day}")

            if include_day:
                with col2:
                    start_hr = st.number_input(f"{day} Start Hour", min_value=6, max_value=20, value=9, key=f"start_{day}")
                with col3:
                    end_hr = st.number_input(f"{day} End Hour", min_value=7, max_value=22, value=17, key=f"end_{day}")
                with col4:
                    total_hours = st.number_input(f"{day} Total Hours", min_value=1, max_value=12, value=8, key=f"total_{day}")

                working_days.append({
                    "day": day,
                    "start_hr": str(int(start_hr)),
                    "end_hr": str(int(end_hr)),
                    "total_hours": str(int(total_hours))
                })

        # Subject Relationship Constraints
        st.subheader("Subject Relationship Constraints")

        if st.session_state.courses:
            subject_names = [course["name"] for course in st.session_state.courses]

            col1, col2 = st.columns(2)

            with col1:
                st.write("**Consecutive Subjects** (must be scheduled together)")
                consecutive_1 = st.selectbox("Subject 1", [""] + subject_names, key="cons_1")
                consecutive_2 = st.selectbox("Subject 2", [""] + subject_names, key="cons_2")

            with col2:
                st.write("**Non-Consecutive Subjects** (cannot be adjacent)")
                non_consecutive_1 = st.selectbox("Subject 1", [""] + subject_names, key="non_cons_1")
                non_consecutive_2 = st.selectbox("Subject 2", [""] + subject_names, key="non_cons_2")

            if st.button("Save Constraints"):
                # Validate relationship constraints
                cons_pair = [consecutive_1, consecutive_2] if consecutive_1 and consecutive_2 else [""]
                noncons_pair = [non_consecutive_1, non_consecutive_2] if non_consecutive_1 and non_consecutive_2 else [""]

                # Simple validation: same subject cannot be both consecutive and non-consecutive
                if cons_pair and cons_pair[0] and noncons_pair and noncons_pair[0]:
                    if set(cons_pair) == set(noncons_pair):
                        st.error("❌ Same pair selected for both consecutive and non-consecutive constraints. Fix selection.")
                    else:
                        st.session_state.constraints = {
                            "working_days": working_days,
                            "consecutive_subjects": cons_pair,
                            "non_consecutive_subjects": noncons_pair
                        }
                        st.success("✅ Constraints saved successfully!")
                else:
                    st.session_state.constraints = {
                        "working_days": working_days,
                        "consecutive_subjects": cons_pair,
                        "non_consecutive_subjects": noncons_pair
                    }
                    st.success("✅ Constraints saved successfully!")
        else:
            st.warning("⚠️ Please add courses first before setting constraints.")

    elif tab == "Generate Timetable":
        st.header("🎯 Generate Timetable")

        if not st.session_state.courses:
            st.error("❌ No courses added. Please add courses first.")
            return

        if not st.session_state.constraints:
            st.error("❌ No constraints set. Please set constraints first.")
            return

        # Display current setup
        col1, col2 = st.columns(2)

        with col1:
            st.subheader("📚 Courses Summary")
            for course in st.session_state.courses:
                st.markdown(f"""
                <div class="constraint-box">
                    <strong>{course['name']}</strong><br>
                    Instructor: {course['instructor_name']}<br>
                    Lectures: {course['lectureno']}/week × {course['duration']}h<br>
                    Available: {course['start_hr']}:00 - {course['end_hr']}:00
                </div>
                """, unsafe_allow_html=True)

        with col2:
            st.subheader("⚙️ Constraints Summary")

            # Working days
            working_days = st.session_state.constraints.get("working_days", [])
            if working_days:
                st.write("**Working Days:**")
                for day in working_days:
                    st.write(f"• {day['day']}: {day['start_hr']}:00-{day['end_hr']}:00 ({day['total_hours']}h)")

            # Subject relationships
            cons_subjects = st.session_state.constraints.get("consecutive_subjects", [""])
            if cons_subjects and cons_subjects[0]:
                st.write(f"**Consecutive:** {cons_subjects[0]} ↔ {cons_subjects[1]}")

            non_cons_subjects = st.session_state.constraints.get("non_consecutive_subjects", [""])
            if non_cons_subjects and non_cons_subjects[0]:
                st.write(f"**Non-consecutive:** {non_cons_subjects[0]} ↮ {non_cons_subjects[1]}")

        # Option: allow filling extra slots with Free periods
        allow_free = st.checkbox("Allow Free Periods (fill extra slots automatically)", value=True)

        # Generate button
        if st.button("🎯 Generate Timetable", type="primary"):
            with st.spinner("Generating timetable using OR-Tools CP-SAT solver..."):
                try:
                    result = generate_timetable_ortools(st.session_state.constraints, st.session_state.courses, allow_free=allow_free, max_time_seconds=15)
                except Exception as e:
                    st.session_state.generated_timetable = None
                    st.session_state.last_error = str(e)
                    st.markdown(f"""
                    <div class="error-box">
                        ❌ <strong>Error during generation!</strong><br>
                        {str(e)}
                    </div>
                    """, unsafe_allow_html=True)
                    return

                if isinstance(result, dict) and result.get('error'):
                    st.session_state.generated_timetable = None
                    st.session_state.last_error = result['error']
                    st.markdown(f"""
                    <div class="error-box">
                        ❌ <strong>No valid timetable found!</strong><br>
                        {result['error']}
                    </div>
                    """, unsafe_allow_html=True)
                else:
                    st.session_state.generated_timetable = result
                    st.session_state.last_error = None
                    st.markdown("""
                    <div class="success-box">
                        ✅ <strong>Timetable generated successfully!</strong><br>
                        Go to "View Results" to see your timetable.
                    </div>
                    """, unsafe_allow_html=True)

    elif tab == "View Results":
        st.header("📋 Generated Timetable")

        if st.session_state.last_error:
            st.warning(f"⚠️ Last run error: {st.session_state.last_error}")

        if not st.session_state.generated_timetable:
            st.warning("⚠️ No timetable generated yet. Please generate a timetable first.")
            return

        timetable = st.session_state.generated_timetable

        # Display timetable in tabs
        days_with_schedule = [day for day, schedule in timetable.items() if schedule]

        if days_with_schedule:
            day_tabs = st.tabs([day.capitalize() for day in days_with_schedule])

            for i, day in enumerate(days_with_schedule):
                with day_tabs[i]:
                    schedule = timetable[day]

                    if schedule:
                        # Create DataFrame for better display
                        df_data = []
                        for item in schedule:
                            df_data.append({
                                "Time Slot": item['slot'],
                                "Subject": item['subject'],
                                "Time": f"{item['start_time']} - {item['end_time']}"
                            })

                        df = pd.DataFrame(df_data)
                        st.dataframe(df, use_container_width=True, hide_index=True)
                    else:
                        st.info(f"No classes scheduled for {day.capitalize()}")
        else:
            st.info("No classes scheduled on any day (empty timetable).")

        # Export functionality
        st.subheader("📤 Export Options")
        col1, col2 = st.columns(2)

        with col1:
            json_str = json.dumps(timetable, indent=2)
            st.download_button(
                label="📥 Download as JSON",
                data=json_str,
                file_name="timetable.json",
                mime="application/json"
            )

        with col2:
            if st.button("🔄 Generate New Timetable"):
                st.session_state.generated_timetable = None
                st.rerun()

if __name__ == "__main__":
    main()
