@login_required(login_url='/staff/login/')
def create_exam(request):
    if not getattr(request.user, 'is_staff_user', False):
        messages.error(request, "Unauthorized access")
        return redirect('dashboard:router')

    if request.method == "POST":
        title = request.POST.get("title")
        subject_id = request.POST.get("subject")
        duration = int(request.POST.get("duration", 0))
        start_time = request.POST.get("start_time")
        end_time = request.POST.get("end_time")

        exam = Exam.objects.create(
            title=title,
            subject_id=subject_id,
            duration_minutes=duration,
            start_time=start_time,
            end_time=end_time,
            created_by=request.user.staff
        )

        # Assign selected classes
        class_ids = request.POST.getlist("classes")
        exam.classes.set(class_ids)
        messages.success(request, f"Exam '{exam.title}' created successfully!")
        return redirect('exam_detail', exam_id=exam.id)

    classes = request.user.staff.get_classes() if hasattr(request.user, 'staff') else []
    return render(request, "cbt/create_exam.html", {"classes": classes})