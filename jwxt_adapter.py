"""Read-only parser for the school's Qiangzhi-style teaching system.

The actual HUE account pages still need a student-led compatibility check.
No password, CAPTCHA, course selection, or grade modification lives here.
"""

import re

from bs4 import BeautifulSoup


def identity(html: str) -> dict | None:
    text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
    number = re.search(r"学生编号\s*[：:]\s*(\d{6,20})", text)
    name = re.search(r"学生姓名\s*[：:]\s*([^\s]{1,30})", text)
    if not number:
        return None
    return {"number": number.group(1), "name": name.group(1) if name else "教务系统用户"}


def grades(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one("#dataList")
    if not table:
        return []
    rows = table.select("tr")
    if not rows:
        return []
    headers = [c.get_text(" ", strip=True).replace(" ", "") for c in rows[0].select("th,td")]

    def column(names):
        return next((i for name in names for i, header in enumerate(headers) if name in header), None)

    def exact_column(names):
        return next((i for name in names for i, header in enumerate(headers) if name == header), None)

    indexes = {"course": column(("课程名称",)), "score": column(("最终成绩", "总评成绩", "成绩")),
               "credit": column(("学分",)), "year": column(("学年",)), "term": column(("学期",)),
               "grade_point": exact_column(("课程绩点", "成绩绩点", "绩点")),
               "exam_type": column(("考试性质", "成绩获取方式", "修读性质"))}
    if indexes["course"] is None or indexes["score"] is None:
        return []
    result = []
    for row in rows[1:]:
        cells = [c.get_text(" ", strip=True) for c in row.select("td")]
        if not cells:
            continue
        value = {key: cells[i][:120] if i is not None and i < len(cells) else "" for key, i in indexes.items()}
        if value["course"]:
            result.append(value)
    return result[:300]


GRADE_POINTS = {
    "优秀": 4.0, "优": 4.0, "良好": 3.0, "良": 3.0,
    "中等": 2.0, "中": 2.0, "及格": 1.0, "不及格": 0.0,
}


def _number(value) -> float | None:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def course_grade_point(score) -> float | None:
    """Apply HUE's 2017 score-to-grade-point rule; unknown labels stay unknown."""
    text = str(score or "").strip()
    number = _number(text)
    if number is not None:
        return 0.0 if number < 60 else min(5.0, (number - 60) / 10 + 1)
    return GRADE_POINTS.get(text)


def _term_key(row: dict) -> str:
    term = str(row.get("term") or "").strip()
    year = str(row.get("year") or "").strip()
    if term and re.search(r"\d{4}\D+\d{4}", term):
        return term
    return "-".join(value for value in (year, term) if value) or "学期未标注"


def _term_label(key: str) -> str:
    match = re.fullmatch(r"(\d{4})-(\d{4})-([12])", key)
    if not match:
        return key
    semester = "第一学期" if match.group(3) == "1" else "第二学期"
    return f"{match.group(1)}-{match.group(2)} 学年 · {semester}"


def _best_records(rows: list[dict]) -> list[dict]:
    """Use the highest result for duplicate course records, matching transcript display rules."""
    best = {}
    for row in rows:
        key = str(row.get("course") or "").strip()
        point = _number(row.get("grade_point"))
        if point is None:
            point = course_grade_point(row.get("score"))
        score = _number(row.get("score"))
        rank = (point if point is not None else -1, score if score is not None else -1)
        if key not in best or rank > best[key][0]:
            best[key] = (rank, row)
    return [item[1] for item in best.values()]


def _prepare_record(raw: dict) -> dict:
    row = dict(raw)
    credit = _number(row.get("credit"))
    official_point = _number(row.get("grade_point"))
    point = official_point if official_point is not None else course_grade_point(row.get("score"))
    row["calculated_grade_point"] = round(point, 2) if point is not None else None
    row["credit_points"] = round(credit * point, 2) if credit is not None and point is not None else None
    exam_type = str(row.get("exam_type") or "").strip()
    score = _number(row.get("score"))
    if "重修" in exam_type:
        row["record_status"] = "重修记录"
    elif "补考" in exam_type:
        row["record_status"] = "补考记录"
    elif score is not None and score < 60:
        row["record_status"] = "不及格"
    else:
        row["record_status"] = "正常考试"
    return row


def _grade_metrics(rows: list[dict]) -> dict:
    attempted = earned = credit_points = numeric_total = 0.0
    numeric_count = 0
    known = passed = 0
    prepared = []
    for raw in rows:
        row = _prepare_record(raw)
        credit = _number(row.get("credit"))
        point = row["calculated_grade_point"]
        score = _number(row.get("score"))
        prepared.append(row)
        if credit is None or credit <= 0 or point is None:
            continue
        attempted += credit
        credit_points += credit * point
        known += 1
        if point > 0:
            earned += credit
            passed += 1
        average_score = score if score is not None else ((point - 1) * 10 + 60 if point > 0 else None)
        if average_score is not None:
            numeric_total += average_score
            numeric_count += 1
    return {
        "courses": prepared, "course_count": len(prepared), "known_course_count": known,
        "attempted_credits": round(attempted, 2), "earned_credits": round(earned, 2),
        "credit_points": round(credit_points, 2),
        "average_gpa": round(credit_points / attempted, 2) if attempted else None,
        "average_score": round(numeric_total / numeric_count, 2) if numeric_count else None,
        "passed_count": passed, "excluded_count": len(prepared) - known,
    }


def grade_summary(grades_rows: list[dict]) -> dict:
    """Return semester blocks plus a transcript-style cumulative summary."""
    grouped = {}
    for row in grades_rows:
        grouped.setdefault(_term_key(row), []).append(row)
    terms = []
    for key in sorted(grouped, reverse=True):
        metrics = _grade_metrics(_best_records(grouped[key]))
        terms.append({"key": key, "label": _term_label(key),
                      "records": [_prepare_record(row) for row in grouped[key]], **metrics})
    overall_rows = _best_records([row for values in grouped.values() for row in values])
    return {"overall": _grade_metrics(overall_rows), "terms": terms,
            "raw_count": len(grades_rows), "counted_count": len(overall_rows)}


def current_week(html: str) -> int | None:
    text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
    match = re.search(r"第\s*(\d{1,2})\s*周", text)
    return int(match.group(1)) if match and 1 <= int(match.group(1)) <= 30 else None


def week_numbers(label: str) -> list[int]:
    match = re.search(r"([\d,，、\-—]+)\s*\(周\)", label)
    if not match:
        return []
    weeks = set()
    for part in re.split(r"[,，、]", match.group(1)):
        bounds = re.split(r"[-—]", part)
        if len(bounds) == 2 and all(value.isdigit() for value in bounds):
            weeks.update(range(int(bounds[0]), int(bounds[1]) + 1))
        elif part.isdigit():
            weeks.add(int(part))
    if "单周" in label:
        weeks = {week for week in weeks if week % 2 == 1}
    if "双周" in label:
        weeks = {week for week in weeks if week % 2 == 0}
    return sorted(week for week in weeks if 1 <= week <= 30)


def schedule(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    tables = soup.select("table")
    table = next((table for table in tables if any(x in table.get_text(" ", strip=True) for x in ("星期一", "周一"))
                  and any(x in table.get_text(" ", strip=True) for x in ("星期二", "周二"))), None)
    if not table:
        return []
    rows = table.select("tr")
    headers = [c.get_text(" ", strip=True) for c in rows[0].select("th,td")]
    days = [next((day for day, marker in enumerate(("一", "二", "三", "四", "五", "六", "日"), 1)
                  if ("星期" + marker) in label or ("周" + marker) in label), None) for label in headers]
    result = []
    for row in rows[1:]:
        cells = row.select("th,td")
        if not cells:
            continue
        period = cells[0].get_text(" ", strip=True)[:80]
        for i, cell in enumerate(cells):
            if i >= len(days) or not days[i]:
                continue
            course_blocks = [node for node in cell.select(".kbcontent") if "sykb2" not in node.get("class", [])]
            if not course_blocks:
                detail = cell.get_text(" ", strip=True)
                if detail:
                    result.append({"day": days[i], "period": period, "detail": detail[:400],
                                   "course": detail[:120], "teacher": "", "room": "", "weeks": []})
                continue
            for block in course_blocks:
                for chunk in re.split(r"-{5,}", block.get_text("\n", strip=True)):
                    lines = [line.strip() for line in chunk.splitlines() if line.strip()]
                    week_line = next((line for line in lines if "(周)" in line), "")
                    if not lines or not week_line:
                        continue
                    teacher = lines[1] if len(lines) > 1 and lines[1] != week_line else ""
                    room = lines[-1] if lines[-1] != week_line else ""
                    course = lines[0][:120]
                    result.append({"day": days[i], "period": period, "detail": " · ".join(
                        value for value in (course, room, teacher) if value)[:400],
                        "course": course, "teacher": teacher[:40], "room": room[:80],
                        "weeks": week_numbers(week_line), "week_label": week_line[:120]})
    return result[:300]
