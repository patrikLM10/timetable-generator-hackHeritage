# Timetable Generator

A Streamlit-based web application for generating conflict-free timetables using constraint satisfaction algorithms.

## Overview

This project provides two implementations of an automated timetable generator:

- **app2.py**: Uses the `python-constraint` library for constraint satisfaction
- **app3.py**: Uses Google's OR-Tools CP-SAT solver for optimized scheduling

Both applications allow users to input courses, teachers, classrooms, and time slots, then automatically generate schedules that respect all constraints (no teacher/classroom conflicts, proper time slot assignments, etc.).

## Features

- 📅 Interactive web interface built with Streamlit
- 🎯 Constraint-based scheduling (no overlapping classes for teachers or rooms)
- 📊 Visual timetable display with pandas DataFrames
- 💾 Export schedules to JSON format
- 🎨 Custom CSS styling with gradient headers and colored status boxes

## Requirements

- Python 3.x
- Streamlit
- pandas
- json (standard library)
- typing (standard library)

### For app2.py:
- python-constraint (`constraint` package)

### For app3.py:
- OR-Tools (`ortools` package)

## Installation

1. Clone the repository:
```bash
git clone <repository-url>
cd timetable-generator-hackHeritage
```

2. Install dependencies:
```bash
pip install streamlit pandas python-constraint ortools
```

## Usage

### Running app2.py (Constraint Library)
```bash
streamlit run app2.py
```

### Running app3.py (OR-Tools)
```bash
streamlit run app3.py
```

The application will open in your default browser where you can:
1. Add courses with their respective teachers
2. Define available classrooms
3. Set time slots
4. Generate a conflict-free timetable

## Project Structure

```
timetable-generator-hackHeritage/
├── README.md          # This file
├── app2.py           # Constraint satisfaction implementation
└── app3.py           # OR-Tools CP-SAT implementation
```

## License

This project is part of the hackHeritage event.