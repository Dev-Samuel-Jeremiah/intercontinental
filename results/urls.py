from django.urls import path
from . import views, views_assessment, views_history, views_release, views_tools

app_name = 'results'

urlpatterns = [
    # ── Staff / Admin ──────────────────────────────────────────────────
    path('bundles/',          views_history.bundles,               name='bundles'),
    path('bundles/<int:pk>/download/', views_history.bundle_download, name='bundle_download'),
    path('progress/<int:student_id>/', views_history.progress,     name='progress'),
    path('ask/<int:student_id>/<int:term_id>/', views_history.query_new, name='query_new'),
    path('questions/',        views_history.queries,               name='queries'),
    path('transcript/<int:student_id>/', views_history.transcript, name='transcript'),
    path('transcript/verify/<str:ref>/', views_history.verify_transcript, name='verify_transcript'),
    path('transcript/verify/<str:ref>/qr.png', views_history.transcript_qr, name='transcript_qr'),
    path('sheet/',            views_release.sheet_action,          name='sheet_action'),
    path('approvals/',        views_release.approvals,             name='approvals'),
    path('release/',          views_release.release_settings,      name='release'),
    path('pins/<int:pk>/',    views_release.pin_batch,             name='pin_batch'),
    path('verify/<str:ref>/', views_release.verify_report,         name='verify'),
    path('verify/<str:ref>/qr.png', views_release.verify_qr,       name='verify_qr'),
    path('grading/',          views_tools.grading,                 name='grading'),
    path('scores/sheet/',     views_tools.score_sheet,             name='score_sheet'),
    path('scores/upload/',    views_tools.score_upload,            name='score_upload'),
    path('broadsheet/',       views_tools.broadsheet,              name='broadsheet'),
    path('assessment/',       views_assessment.assessment_settings, name='assessment'),
    path('term/',             views.term_results,                  name='term_results'),
    path('mine/',             views.my_results,                    name='my_results'),
    path('manual-scores/',    views.manual_score_entry,            name='manual_score_entry'),
    path('cbt-scores/',       views.cbt_scores_reference,          name='cbt_scores_reference'),
    path('cumulative/',       views.cumulative_results,            name='cumulative_results'),
    path('generate-session/', views.generate_session_results_view, name='generate_session_results'),
    path('publish-results/',  views.publish_results_page,          name='publish_results_page'),
    path('batch-results/',    views.batch_results,                 name='batch_results'),

    # ── Parent ─────────────────────────────────────────────────────────
    path('dashboard/',        views.parent_view_results, name='dashboard'),
    path(
        'results/<int:student_id>/<int:session_id>/<int:term_id>/',
        views.parent_result_detail,
        name='result_detail',
    ),

    # ── Student ────────────────────────────────────────────────────────
    path(
        'results/<int:session_id>/<int:term_id>/',
        views.student_result_detail,
        name='student_result_detail',
    ),

    # ── AJAX / autosave endpoints ──────────────────────────────────────
    path('save-teacher-remark/',   views.save_teacher_remark,   name='save_teacher_remark'),
    path('save-subject-remark/',   views.save_subject_remark,   name='save_subject_remark'),
    path('save-hos-remark/',       views.save_hos_remark,       name='save_hos_remark'),
    path('save-skill-assessment/', views.save_skill_assessment, name='save_skill_assessment'),
    path('attendance/',            views.manual_term_attendance, name='manual_term_attendance'),

    # ── AI Comment endpoints ───────────────────────────────────────────
    # Single student — called from the result card page
    path('ai-comment/generate/',      views.generate_ai_comment,        name='generate_ai_comment'),
    # Whole class bulk generation — called from an admin/class management page
    path('ai-comment/generate-bulk/', views.generate_ai_comments_bulk,  name='generate_ai_comments_bulk'),
    # GET existing comment to prefill on page load
    path('ai-comment/get/',           views.get_ai_comment,             name='get_ai_comment'),
]