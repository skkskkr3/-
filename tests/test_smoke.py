import unittest
import app as app_module
from fastapi import HTTPException
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from app import app, belongs_to_current_semester, browser_profile_ready, course_name_score, current_semester_data, identity_from_context, now, parse_all_task, parse_chaoxing_answer_window, parse_chaoxing_courses, parse_chaoxing_profile, parse_chaoxing_session, queue_sync, valid_semester_schedule
import jwxt_adapter


class SmokeTests(unittest.TestCase):
    def test_profile_parser(self):
        html = '<script>var uid = 12345; var fid = 678;</script><span aria-label="账号：测试用户"></span>'
        self.assertEqual(parse_chaoxing_profile(html), {
            "uid": "12345", "name": "测试用户", "school": "", "fid": "678",
        })

    def test_profile_uses_current_logged_in_page(self):
        class NoRequestContext:
            @property
            def request(self):
                raise AssertionError("当前页面已有身份，不应请求其他站点")

        html = '<script>var uid = 12345; var fid = 678;</script><span aria-label="账号：测试用户"></span>'
        self.assertEqual(identity_from_context(NoRequestContext(), html)["uid"], "12345")

    def test_profile_can_use_chaoxing_session_without_exposing_passwords(self):
        cookies = [{"name": "UID", "value": "12345", "domain": ".chaoxing.com"},
                   {"name": "fid", "value": "678", "domain": ".chaoxing.com"},
                   {"name": "unrelated", "value": "secret", "domain": ".example.com"}]
        self.assertEqual(parse_chaoxing_session(cookies), {
            "uid": "12345", "name": "学习通用户", "school": "", "fid": "678",
        })

    def test_auto_sync_queues_once_and_manual_refresh_can_override_fresh_cache(self):
        states = {}
        with patch("app.threading.Thread") as thread:
            self.assertTrue(queue_sync(1, states, lambda uid: None, "自动读取"))
            self.assertFalse(queue_sync(1, states, lambda uid: None, "自动读取"))
            states[1] = {"state": "done", "updated_at": now()}
            self.assertFalse(queue_sync(1, states, lambda uid: None, "自动读取"))
            self.assertTrue(queue_sync(1, states, lambda uid: None, "手动读取", force=True))
            states[1] = {"state": "needs_reauth", "updated_at": now()}
            self.assertFalse(queue_sync(1, states, lambda uid: None, "自动读取"))
            self.assertEqual(thread.return_value.start.call_count, 2)

    def test_saved_storage_state_counts_as_connected_profile(self):
        with TemporaryDirectory() as folder:
            profile = Path(folder)
            self.assertFalse(browser_profile_ready(profile))
            (profile / "storage_state.json").write_text("{}", encoding="utf-8")
            self.assertTrue(browser_profile_ready(profile))

    def test_course_parser(self):
        html = '<a href="/visit/stucoursemiddle?courseid=1&amp;clazzid=2&amp;cpi=3"><span class="course-name" title="测试课程"></span></a>'
        courses = parse_chaoxing_courses(html)
        self.assertEqual(len(courses), 1)
        self.assertEqual(courses[0]["name"], "测试课程")
        self.assertEqual(courses[0]["external_id"], "1:2:3")

    def test_current_task_list_parser(self):
        html = '''<ul class="task-list"><li onclick="goTask(this);" data="https://mooc1-api.chaoxing.com/mooc-ans/mooc2/work/task?workId=1">
        <div class="right-content"><p class="overHidden2 fl">实验一</p><span class="stuStatus">待完成</span>
        <span class="courseName">测试课程</span><div class="time notOver">09-30 23:59</div></div></li></ul>'''
        tasks = parse_all_task(html)
        self.assertEqual((len(tasks), tasks[0]["title"], tasks[0]["course"]), (1, "实验一", "测试课程"))
        self.assertIsNotNone(tasks[0]["due_at"])
        self.assertEqual(tasks[0]["platform_status"], "unfinished")

    def test_assignment_answer_window(self):
        published, due = parse_chaoxing_answer_window('<p>作答时间：09-01 08:00 至 09-30 23:59</p>')
        self.assertIsNotNone(published)
        self.assertIsNotNone(due)
        self.assertLess(published, due)

    def test_old_password_routes_removed(self):
        paths = {route.path for route in app.routes}
        self.assertNotIn("/api/login", paths)
        self.assertNotIn("/api/register", paths)
        self.assertIn("/api/chaoxing/login/start", paths)
        self.assertIn("/api/jwxt/login/start", paths)
        self.assertIn("/api/chaoxing/login/cancel/{ticket}", paths)
        self.assertIn("/api/jwxt/login/cancel/{ticket}", paths)

    def test_stale_login_is_not_reused_and_can_be_cancelled(self):
        stale = app_module.new_login_state("等待", None)
        stale["state"] = "waiting"
        stale["started_monotonic"] -= app_module.LOGIN_TIMEOUT_SECONDS + 1
        states = {"a" * 32: stale}
        self.assertIsNone(app_module.reusable_login(states, None, False))
        self.assertTrue(stale["cancel_event"].is_set())
        fresh = app_module.new_login_state("等待", None)
        fresh["state"] = "waiting"
        states = {"b" * 32: fresh}
        self.assertEqual(app_module.reusable_login(states, None, False), "b" * 32)
        self.assertEqual(app_module.cancel_login(states, "b" * 32), {"ok": True})
        self.assertTrue(fresh["cancel_event"].is_set())

    def test_only_one_platform_login_can_wait_at_a_time(self):
        request = MagicMock()
        request.headers = {}
        other = app_module.new_login_state("等待教务登录", None)
        other["state"] = "waiting"
        app_module._login_state.clear()
        app_module._jwxt_login_state.clear()
        app_module._jwxt_login_state["c" * 32] = other
        try:
            with self.assertRaises(HTTPException) as error:
                app_module.start_chaoxing_login(request, session=None)
            self.assertEqual(error.exception.status_code, 409)
            with patch("app.threading.Thread"):
                result = app_module.start_chaoxing_login(request, force=True, session=None)
            self.assertRegex(result["ticket"], r"^[0-9a-f]{32}$")
            self.assertTrue(other["cancel_event"].is_set())
            self.assertFalse(app_module._jwxt_login_state)
        finally:
            app_module._login_state.clear()
            app_module._jwxt_login_state.clear()

    def test_manual_task_can_be_deleted(self):
        conn = MagicMock()
        context = MagicMock()
        context.__enter__.return_value = conn
        with patch.object(app_module, "require_user", return_value=7), \
             patch.object(app_module, "db", return_value=context), \
             patch.object(app_module, "get_assignment", return_value={"source": "manual"}), \
             patch.object(app_module.shutil, "rmtree") as rmtree:
            self.assertEqual(app_module.delete_task(9, "session"), {"ok": True})
        statements = [call.args[0] for call in conn.execute.call_args_list]
        self.assertTrue(any("DELETE FROM assignments" in statement for statement in statements))
        rmtree.assert_called_once()

    def test_jwxt_read_only_parsers(self):
        home = '<p>学生姓名：测试同学</p><p>学生编号：1234567890</p>'
        self.assertEqual(jwxt_adapter.identity(home), {"number": "1234567890", "name": "测试同学"})
        grades = '<table id="dataList"><tr><th>学年</th><th>学期</th><th>课程名称</th><th>学分</th><th>成绩</th></tr><tr><td>2025-2026</td><td>1</td><td>测试课程</td><td>3</td><td>88</td></tr></table>'
        self.assertEqual(jwxt_adapter.grades(grades)[0]["score"], "88")
        schedule = '<table><tr><th>节次</th><th>星期一</th><th>星期二</th></tr><tr><td>1-2节</td><td>测试课程 教室101</td><td></td></tr></table>'
        parsed = jwxt_adapter.schedule(schedule)[0]
        self.assertEqual((parsed["day"], parsed["period"], parsed["detail"]), (1, "1-2节", "测试课程 教室101"))
        rich = '''<table><tr><th>节次</th><th>星期一</th><th>星期二</th></tr><tr><td>1-2节</td><td>
        <div class="kbcontent">计算机网络<br><font title="老师">刘老师</font><br>
        <font title="周次(节次)">1-16(周)[01-02节]</font><br><font title="教室">BY506</font></div></td><td></td></tr></table>'''
        course = jwxt_adapter.schedule(rich)[0]
        self.assertEqual((course["course"], course["teacher"], course["room"]), ("计算机网络", "刘老师", "BY506"))
        self.assertEqual(course["weeks"], list(range(1, 17)))
        self.assertEqual(jwxt_adapter.current_week("<p>第4周/22周</p>"), 4)

    def test_grade_summary_uses_school_gpa_rules_and_highest_duplicate(self):
        rows = [
            {"course": "程序设计", "score": "55", "credit": "3", "term": "2024-2025-1"},
            {"course": "程序设计", "score": "80", "credit": "3", "term": "2024-2025-1"},
            {"course": "军事技能", "score": "良", "credit": "2", "term": "2024-2025-1"},
            {"course": "高等数学", "score": "90", "credit": "5", "term": "2024-2025-2"},
        ]
        report = jwxt_adapter.grade_summary(rows)
        self.assertEqual((report["raw_count"], report["counted_count"]), (4, 3))
        self.assertEqual(jwxt_adapter.course_grade_point("80"), 3.0)
        self.assertEqual(jwxt_adapter.course_grade_point("良"), 3.0)
        self.assertEqual(report["overall"]["earned_credits"], 10.0)
        self.assertEqual(report["overall"]["credit_points"], 35.0)
        self.assertEqual(report["overall"]["average_gpa"], 3.5)
        self.assertEqual(report["overall"]["average_score"], 83.33)
        self.assertEqual(report["terms"][0]["label"], "2024-2025 学年 · 第二学期")
        self.assertEqual(report["terms"][1]["course_count"], 2)
        self.assertIn("正常考试", {row["record_status"] for row in report["terms"][1]["records"]})

    def test_grade_parser_reads_official_point_when_available(self):
        html = '<table id="dataList"><tr><th>课程名称</th><th>学分</th><th>成绩</th><th>绩点</th><th>考试性质</th></tr><tr><td>测试课程</td><td>2</td><td>88</td><td>3.8</td><td>正常考试</td></tr></table>'
        row = jwxt_adapter.grades(html)[0]
        self.assertEqual((row["grade_point"], row["exam_type"]), ("3.8", "正常考试"))

    def test_grade_summary_matches_official_retake_semester_totals(self):
        rows = [
            {"course": "课程A", "score": "66", "credit": "3", "grade_point": "1.6"},
            {"course": "课程B", "score": "90", "credit": "1", "grade_point": "4"},
            {"course": "国家安全教育", "score": "0", "credit": "1", "grade_point": "0", "exam_type": "重修一"},
            {"course": "国家安全教育", "score": "20", "credit": "1", "grade_point": "0", "exam_type": "正常考试"},
            {"course": "国家安全教育", "score": "0", "credit": "1", "grade_point": "0", "exam_type": "补考一"},
            {"course": "军事技能", "score": "良", "credit": "2", "grade_point": "3.5"},
        ]
        metrics = jwxt_adapter.grade_summary(rows)["overall"]
        self.assertEqual((metrics["course_count"], metrics["attempted_credits"]), (4, 7.0))
        self.assertEqual(metrics["average_score"], 65.25)

    def test_current_semester_courses_merge_without_deleting_history(self):
        courses = [
            {"id": 1, "name": "《算法设计与分析》", "url": "https://example.com/current"},
            {"id": 2, "name": "数据结构", "url": "https://example.com/old"},
        ]
        tasks = [
            {"id": 10, "course_id": 1, "platform_status": "unfinished", "source": "chaoxing"},
            {"id": 11, "course_id": 2, "platform_status": "completed", "source": "chaoxing"},
        ]
        schedule = [{"course": "算法设计与分析", "day": 1, "period": "1-2节", "teacher": "刘老师", "room": "A101"}]
        semester_courses, semester_tasks = current_semester_data(courses, tasks, schedule)
        self.assertEqual((len(semester_courses), semester_courses[0]["id"], semester_courses[0]["unfinished_count"]), (1, 1, 1))
        self.assertEqual([task["id"] for task in semester_tasks], [10])
        self.assertEqual(course_name_score("专业实训Ⅳ", "专业实训Ⅰ_2024级"), 0)
        self.assertTrue(belongs_to_current_semester("《算法设计与分析》", ["算法设计与分析", "机器学习"]))
        self.assertFalse(belongs_to_current_semester("数据结构", ["算法设计与分析", "机器学习"]))

    def test_invalid_timetable_tooltip_is_hidden(self):
        schedule = [{"course": "机器学习"}, {"course": "课程甲;教师;课程乙;教师"}]
        self.assertEqual(valid_semester_schedule(schedule), [{"course": "机器学习"}])


if __name__ == "__main__":
    unittest.main()
