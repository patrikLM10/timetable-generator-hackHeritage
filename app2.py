import streamlit as st
from constraint import Problem
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

def get_time_slots(slot_dict: Dict[str, int], start_times: Dict[str, int]) -> Tuple[List[str], Dict[str,int], Dict[str,str], Dict[str,int]]:
    """
    Generate time slots based on working days and hours.
    Returns:
      - slot_names: ordered list of slot ids (variable order used by CSP)
      - slot_time: mapping slot_id -> start hour (int)
      - slot_to_day: mapping slot_id -> day in lowercase (e.g., 'monday')
      - day_slot_counts: mapping day.lower() -> number of slots for that day
    Notes:
      - Creates exactly `total_hours` slots per day.
      - Skips the lunch hour by adding 2 when current hour == 12 (consistent with your original logic).
    """
    slot_names: List[str] = []
    slot_time: Dict[str, int] = {}
    slot_to_day: Dict[str, str] = {}
    day_slot_counts: Dict[str, int] = {}

    day_abbreviations = {
        'Monday': 'M', 'Tuesday': 'T', 'Wednesday': 'W',
        'Thursday': 'Th', 'Friday': 'F', 'Saturday': 'Sa', 'Sunday': 'Su'
    }

    for day, hours in slot_dict.items():
        hours = int(hours)
        start = int(start_times[day])
        abbrev = day_abbreviations.get(day, day[:2])
        day_count = 0

        for j in range(hours):  # create exactly `hours` slots
            slot_name = f"{abbrev}{j + 1}"
            slot_names.append(slot_name)
            slot_time[slot_name] = start
            slot_to_day[slot_name] = day.lower()
            day_count += 1

            # increment hour; if we hit lunch (12), skip next hour (increase by 2)
            if start == 12:
                start += 2
            else:
                start += 1

        day_slot_counts[day.lower()] = day_count

    return slot_names, slot_time, slot_to_day, day_slot_counts

def generate_timetable(constraints: Dict[str, Any], courses: List[Dict[str, Any]], allow_free: bool = True) -> Any:
    """
    Generate timetable using CSP. Returns:
      - On success: dict mapping days (lowercase) to list of schedule entries.
      - On failure: dict with {'error': "message"} so UI can present a helpful message.
    """
    if not constraints or not courses:
        return {'error': "No constraints or courses provided."}

    # Build working_day dict and start_times
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
    subject_required_slots: Dict[str, int] = {}
    subject_info: Dict[str, Dict[str,int]] = {}
    multi_slot_subjects: Dict[str, int] = {}

    for course in courses:
        name = course["name"]
        lect_no = int(course['lectureno'])
        duration = int(course['duration'])
        required = lect_no * duration
        subject_required_slots[name] = required
        subject_info[name] = {
            'start_hr': int(course['start_hr']),
            'end_hr': int(course['end_hr']),
            'duration': duration
        }
        if duration > 1:
            multi_slot_subjects[name] = duration

    # Build time slots
    time_slots, slot_time, slot_to_day, day_slot_counts = get_time_slots(slot_counts_by_day, start_times)

    total_available_slots = len(time_slots)
    total_required_slots = sum(subject_required_slots.values())

    # If there are more available slots than required, either allow free periods or throw error
    if total_available_slots > total_required_slots:
        diff = total_available_slots - total_required_slots
        if allow_free:
            # Add a pseudo-subject 'Free' to fill remaining slots
            free_name = "Free"
            # ensure unique name in case a real course named 'Free' exists
            cnt = 1
            while free_name in subject_required_slots:
                free_name = f"Free_{cnt}"
                cnt += 1
            subject_required_slots[free_name] = diff
            # free slots have open availability (0-24) and duration 1
            subject_info[free_name] = {'start_hr': 0, 'end_hr': 24, 'duration': 1}
            # Free is not in multi_slot_subjects
        else:
            return {'error': f"Total available slots ({total_available_slots}) != total required subject-slots ({total_required_slots}). Adjust working hours or course lecture counts, or enable 'Allow Free Periods'."}
    elif total_available_slots < total_required_slots:
        return {'error': f"Total available slots ({total_available_slots}) < total required subject-slots ({total_required_slots}). Increase working hours or reduce lecture counts."}

    # Build CSP problem
    problem = Problem()
    course_names = list(subject_required_slots.keys())
    problem.addVariables(time_slots, course_names)

    # Helper: build slot index -> day mapping for boundary checks
    slot_index_to_day: Dict[int, str] = {}
    for idx, s in enumerate(time_slots):
        slot_index_to_day[idx] = slot_to_day[s]

    # Constraint: each subject must appear exactly required number of times
    def every_subject_constraint(*timetable_values):
        for subj, needed in subject_required_slots.items():
            if timetable_values.count(subj) != needed:
                return False
        return True

    # Constraint: multi-slot subjects must occupy consecutive slots and not cross day boundary
    def multi_slot_constraint(*timetable_values):
        if not multi_slot_subjects:
            return True
        for subj, duration in multi_slot_subjects.items():
            indices = [i for i, s in enumerate(timetable_values) if s == subj]
            if not indices:
                return False
            # check that occurrences are grouped into blocks of size `duration`
            # and blocks do not cross day boundaries
            indices_sorted = sorted(indices)
            # chunk into consecutive runs
            i = 0
            runs = []
            while i < len(indices_sorted):
                run = [indices_sorted[i]]
                i += 1
                while i < len(indices_sorted) and indices_sorted[i] == run[-1] + 1:
                    run.append(indices_sorted[i])
                    i += 1
                runs.append(run)
            # each run must have length equal to duration
            for run in runs:
                if len(run) != duration:
                    return False
                # check all indices in run are same day
                if not all(slot_index_to_day[run[0]] == slot_index_to_day[r] for r in run):
                    return False
        return True

    # Constraint: teacher availability per-slot
    def teacher_availability_constraint(*timetable_values):
        for subj, info in subject_info.items():
            indexes = [i for i, s in enumerate(timetable_values) if s == subj]
            for i in indexes:
                slot_name = time_slots[i]
                start_hr = slot_time[slot_name]
                # slot must start at or after start_hr and strictly before end_hr
                if start_hr < info['start_hr'] or start_hr >= info['end_hr']:
                    return False
        return True

    # Constraint: user-defined consecutive subjects pair must be adjacent
    def user_consecutive_pair_constraint(*timetable_values):
        cons = constraints.get('consecutive_subjects') or []
        if not cons or not cons[0]:
            return True
        if len(cons) < 2:
            return True
        a, b = cons[0], cons[1]
        if a == b:
            return True
        for i, s in enumerate(timetable_values):
            if s == a:
                if not ((i > 0 and timetable_values[i-1] == b) or (i < len(timetable_values)-1 and timetable_values[i+1] == b)):
                    return False
            if s == b:
                if not ((i > 0 and timetable_values[i-1] == a) or (i < len(timetable_values)-1 and timetable_values[i+1] == a)):
                    return False
        return True

    # Constraint: user-defined non-consecutive pair must NOT be adjacent
    def user_non_consecutive_pair_constraint(*timetable_values):
        noncons = constraints.get('non_consecutive_subjects') or []
        if not noncons or not noncons[0]:
            return True
        if len(noncons) < 2:
            return True
        a, b = noncons[0], noncons[1]
        if a == b:
            return True
        for i, s in enumerate(timetable_values):
            if s == a:
                if (i > 0 and timetable_values[i-1] == b) or (i < len(timetable_values)-1 and timetable_values[i+1] == b):
                    return False
            if s == b:
                if (i > 0 and timetable_values[i-1] == a) or (i < len(timetable_values)-1 and timetable_values[i+1] == a):
                    return False
        return True

    # Add constraints
    problem.addConstraint(every_subject_constraint, time_slots)
    if multi_slot_subjects:
        problem.addConstraint(multi_slot_constraint, time_slots)
    problem.addConstraint(teacher_availability_constraint, time_slots)
    problem.addConstraint(user_consecutive_pair_constraint, time_slots)
    problem.addConstraint(user_non_consecutive_pair_constraint, time_slots)

    # Solve: get one solution
    solution = problem.getSolution()
    if solution is None:
        return {'error': "No valid timetable found with the given constraints. Try relaxing constraints or double-check availability/hours."}

    # Build response dict
    resp_data = {d.lower(): [] for d in ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]}
    for slot_name, subject in solution.items():
        day = slot_to_day[slot_name]
        start_hr = slot_time[slot_name]
        end_hr = start_hr + 1
        resp_data[day].append({
            'slot': slot_name,
            'subject': subject,
            'start_time': f"{start_hr:02d}:00",
            'end_time': f"{end_hr:02d}:00"
        })

    # Sort each day by start_time
    for day_key in resp_data:
        resp_data[day_key].sort(key=lambda x: x['start_time'])

    return resp_data

def main():
    initialize_session_state()

    # Header
    st.markdown("""
    <div class="main-header">
        <h1>📅 Time Table Generator</h1>
        <p>Dynamic scheduling system using Constraint Satisfaction Problem (CSP)</p>
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
                start_hr = st.number_input("Instructor Start Hour", min_value=8, max_value=20, value=9)
                end_hr = st.number_input("Instructor End Hour", min_value=9, max_value=21, value=17)

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
            with st.spinner("Generating timetable using CSP algorithm..."):
                result = generate_timetable(st.session_state.constraints, st.session_state.courses, allow_free=allow_free)

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
