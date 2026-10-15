@login_required
def manual_score_entry(request):
    active_session = get_active_session()

    classes  = Class.objects.all()
    terms    = Term.objects.all()

    selected_class   = request.GET.get('class')
    selected_subject = request.GET.get('subject')
    selected_term    = request.GET.get('term')

    subjects = Subject.objects.all()
    if selected_class:
        subjects = Subject.objects.filter(
            classsubject__school_class_id=selected_class
        ).distinct()

    rows = []
    class_totals = {
        "ca1":   0,
        "ca2":   0,
        "ca3":   0,
        "exam":  0,
        "total": 0,
        "count": 0,
    }

    # ── FETCH DATA ──────────────────────────────────────────────────
    if selected_class and selected_subject and selected_term:
        students = Student.objects.filter(class_assigned_id=selected_class)

        for student in students:
            tr, created = TermResult.objects.get_or_create(
                student=student,
                class_assigned_id=selected_class,
                subject_id=selected_subject,
                session=active_session,
                term_id=selected_term
            )

            rows.append({"student": student, "tr": tr})

            class_totals["ca1"]   += tr.ca1_score   or 0
            class_totals["ca2"]   += tr.ca2_score   or 0
            class_totals["ca3"]   += tr.ca3_score   or 0
            class_totals["exam"]  += tr.exam_score  or 0
            class_totals["total"] += tr.total_score or 0
            class_totals["count"] += 1

    # ── SAVE MANUAL ENTRIES ─────────────────────────────────────────
    if request.method == "POST":
        def parse(val):
            try:
                return float(val or 0)
            except (ValueError, TypeError):
                return 0.0

        for key in request.POST:
            if key.startswith("ca1_"):
                tr_id = key.split("_")[1]
                try:
                    tr = TermResult.objects.get(id=tr_id)
                except TermResult.DoesNotExist:
                    continue

                tr.ca1_score   = parse(request.POST.get(f"ca1_{tr_id}"))
                tr.ca2_score   = parse(request.POST.get(f"ca2_{tr_id}"))
                tr.ca3_score   = parse(request.POST.get(f"ca3_{tr_id}"))
                tr.exam_score  = parse(request.POST.get(f"exam_{tr_id}"))
                tr.essay_score = 0

                # ✅ Lock raw_exam_score the very first time it is set
                # Once it has a value > 0, it is never overwritten again
                if (tr.raw_exam_score is None or tr.raw_exam_score == 0) and tr.exam_score > 0:
                    tr.raw_exam_score = tr.exam_score

                tr.save()

        messages.success(request, "Scores updated successfully!")
        return redirect(
            f"{request.path}?class={selected_class}"
            f"&subject={selected_subject}&term={selected_term}"
        )

    # ── CLASS AVERAGES ───────────────────────────────────────────────
    class_avg = {}
    if class_totals["count"] > 0:
        class_avg = {
            "ca1":   round(class_totals["ca1"]   / class_totals["count"], 2),
            "ca2":   round(class_totals["ca2"]   / class_totals["count"], 2),
            "ca3":   round(class_totals["ca3"]   / class_totals["count"], 2),
            "exam":  round(class_totals["exam"]  / class_totals["count"], 2),
            "total": round(class_totals["total"] / class_totals["count"], 2),
        }

    return render(request, "results/manual_score_entry.html", {
        "classes":          classes,
        "subjects":         subjects,
        "terms":            terms,
        "rows":             rows,
        "selected_class":   selected_class,
        "selected_subject": selected_subject,
        "selected_term":    selected_term,
        "class_avg":        class_avg,
    })


@login_required
def teacher_score_view(request):
    active_session = get_active_session()

    selected_class   = request.GET.get("class")
    selected_subject = request.GET.get("subject")
    selected_term    = request.GET.get("term")
    selected_student = request.GET.get("student")
    sort_by          = request.GET.get("sort", "name")

    classes  = Class.objects.all().order_by("name")
    terms    = Term.objects.all().order_by("-id")
    subjects = Subject.objects.all().order_by("name")
    students = Student.objects.all().order_by("full_name")

    if selected_class:
        subjects = Subject.objects.filter(
            classsubject__school_class_id=selected_class
        ).distinct().order_by("name")
        students = students.filter(class_assigned_id=selected_class)

    term    = get_active_term()
    session = active_session

    if selected_term:
        term    = get_object_or_404(Term, id=selected_term)
        session = term.session

    rows         = []
    class_stats  = {}
    grading_list = Grading.objects.all().order_by("-min_score")
    subject_obj  = None
    class_obj    = None

    if selected_class and selected_subject and selected_term:
        subject_obj = get_object_or_404(Subject, id=selected_subject)
        class_obj   = get_object_or_404(Class,   id=selected_class)

        qs = TermResult.objects.filter(
            class_assigned_id=selected_class,
            subject_id=selected_subject,
            term=term,
            session=session,
        ).select_related("student", "subject")

        if selected_student:
            qs = qs.filter(student_id=selected_student)

        if sort_by == "score_desc":
            qs = qs.order_by("-total_score")
        elif sort_by == "score_asc":
            qs = qs.order_by("total_score")
        else:
            qs = qs.order_by("student__full_name")

        for r in qs:
            score   = r.total_score or 0
            grading = next((g for g in grading_list if score >= g.min_score), None)

            # ✅ Always show the locked original exam score
            # Falls back to exam_score if raw was never explicitly set
            display_exam = r.raw_exam_score if (r.raw_exam_score is not None and r.raw_exam_score > 0) else r.exam_score or 0

            rows.append({
                "student":      r.student,
                "exam":         display_exam,
                "total":        int(round(score)),
                "grade":        grading.grade       if grading else "–",
                "remark":       grading.description if grading else "–",
                "published":    r.published,
            })

        if rows:
            totals = [r["total"] for r in rows]
            passed = [t for t in totals if t >= 40]
            class_stats = {
                "count":     len(rows),
                "highest":   max(totals),
                "lowest":    min(totals),
                "average":   round(sum(totals) / len(totals), 1),
                "pass_rate": round(len(passed) / len(totals) * 100, 1),
            }

    context = {
        "classes":          classes,
        "subjects":         subjects,
        "terms":            terms,
        "students":         students,
        "rows":             rows,
        "class_stats":      class_stats,
        "selected_class":   selected_class,
        "selected_subject": selected_subject,
        "selected_term":    selected_term,
        "selected_student": selected_student,
        "sort_by":          sort_by,
        "term":             term,
        "session":          session,
        "subject_obj":      subject_obj,
        "class_obj":        class_obj,
    }

    return render(request, "results/teacher_score_view.html", context)