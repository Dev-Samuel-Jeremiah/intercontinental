from django.contrib import admin

from .models import Accident, HealthPlan, Immunisation, Incident, MedicalProfile, Resource, Team, Threshold

# Counselling notes and safeguarding concerns are deliberately not registered here:
# they are only readable through the restricted welfare pages.
admin.site.register([HealthPlan, MedicalProfile, Immunisation, Accident, Incident, Threshold, Team, Resource])
