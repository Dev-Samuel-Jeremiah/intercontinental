from django.contrib import admin

from .models import Book, Branch, Charge, Copy, EResource, Loan, Reservation


class CopyInline(admin.TabularInline):
    model = Copy
    extra = 0


@admin.register(Book)
class BookAdmin(admin.ModelAdmin):
    list_display = ('title', 'authors', 'isbn', 'subject', 'tenant')
    search_fields = ('title', 'authors', 'isbn')
    inlines = [CopyInline]


admin.site.register([Branch, Loan, Charge, Reservation, EResource])
