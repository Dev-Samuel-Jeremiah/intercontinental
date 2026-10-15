from django.contrib import admin

from .models import AIDraft, AISettings, AIUsage, AnnouncementTranslation


@admin.register(AIUsage)
class AIUsageAdmin(admin.ModelAdmin):
    list_display = ['created_at', 'tenant', 'feature', 'provider', 'model', 'input_tokens', 'output_tokens', 'ok']
    list_filter = ['provider', 'feature', 'ok']


admin.site.register(AISettings)
admin.site.register(AIDraft)
admin.site.register(AnnouncementTranslation)

from .models import AdminQuery, EssayFeedback, StudyPlan, TutorMessage, TutorSettings  # noqa: E402

for _m in (TutorSettings, TutorMessage, EssayFeedback, StudyPlan, AdminQuery):
    admin.site.register(_m)
