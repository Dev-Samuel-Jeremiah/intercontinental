"""Starter help-centre articles and product tours (loaded by `manage.py seed_help`)."""

ARTICLES = [
    {'slug': 'first-day-setup', 'title': 'Setting up your school on day one', 'category': 'Getting started',
     'summary': 'The order that saves you time: session, classes, subjects, staff, students.',
     'body': """Most schools are fully set up in an afternoon. Work in this order:

## 1. Check the session and term
A session and first term were created for you. Open Academics and adjust the dates if needed.

## 2. Add classes and subjects
- Add each class (e.g. JSS 1A) and give it a year group so arms share a level.
- Add your subjects, then link subjects to classes.

## 3. Add staff
Add teachers and other staff. Give each the role and permissions they need — for example, finance access only for the bursary.

## 4. Add students
Add students one by one or import them from a spreadsheet. Parents get portal access with the admission number.

## 5. Try it with sample data first
Not ready to enter real records? Load sample data from Account → Data & export, explore, then remove it with one click."""},
    {'slug': 'publishing-results', 'title': 'Entering scores and publishing results', 'category': 'Results',
     'module': 'results', 'summary': 'From score entry to report cards parents can see.',
     'body': """## Entering scores
Teachers enter continuous assessment and exam scores for their subjects. The system adds them up and works out grades and positions.

## Checking
Form teachers and administrators review the broadsheet before anything is released.

## Publishing
Publishing makes report cards visible to students and parents on the portal. You can unpublish if a correction is needed.

- Only staff with "publish results" permission can publish.
- Parents with unpaid fees can be blocked from viewing results if your school chooses."""},
    {'slug': 'fees-and-wallet', 'title': 'Fees, the family wallet and online payments', 'category': 'Finance',
     'module': 'finance', 'summary': 'How parents pay and how the bursary keeps track.',
     'body': """Each family has one wallet shared by all its children. Parents top it up online with Paystack (card, transfer or USSD) or upload proof of a bank payment for the bursary to confirm.

Fees, transport bills, library charges and shop purchases are paid from the wallet, so the bursary sees every naira in one place.

- Set up fee structures per class and term in Finance.
- Send reminders by SMS or email to families who owe.
- Download receipts and statements at any time."""},
    {'slug': 'attendance-daily', 'title': 'Taking attendance and alerting parents', 'category': 'Attendance',
     'summary': 'Daily registers, lateness and automatic absence messages.',
     'body': """Form teachers mark the register each morning on a phone or computer. Students can also be scanned in at the gate with their ID card.

Parents of absent children can be texted automatically once the register is closed. Attendance trends and persistent absence appear on the student's profile and in Analytics."""},
    {'slug': 'library-desk', 'title': 'Lending and returning library books', 'category': 'Library',
     'module': 'library', 'summary': 'Scan the card, scan the book.',
     'body': """At the library desk, scan the borrower's ID card (or type an admission number), then scan each book's barcode to lend it. To return a book, just scan it.

- Late fines are worked out and charged automatically.
- A returned book that someone reserved is set aside and the reader is told.
- Print barcode labels from each book's page in the catalogue."""},
    {'slug': 'school-bus', 'title': 'Running the school bus', 'category': 'Transport', 'module': 'transport',
     'summary': 'Routes, riders, scans and the parents\' bus page.',
     'body': """Set up vehicles and drivers, then routes with their stops and riders.

Drivers and attendants open today's trip on their phone, scan children on and off, and can send a delay message with one tap. Parents see the bus on a map while the trip page is open, and get a text when their child gets on and off."""},
    {'slug': 'export-your-data', 'title': 'Exporting data and backups', 'category': 'Administration',
     'summary': "Download all of the school's data at any time.",
     'body': """Go to Administration & Help → Data Export & Backups and press "Prepare an export". You get a ZIP file with one spreadsheet per table.

A backup is taken automatically every night and the last 14 are kept. Download one regularly and keep it somewhere safe, away from the server."""},
    {'slug': 'school-sections', 'title': 'Sections: Nursery & Primary, Secondary and Darun', 'category': 'Administration',
     'summary': 'Put classes and staff in the right section.',
     'body': """Open Administration & Help → Sections. Each class belongs to one section; a staff member can work in several.

## Steps
- Pick the section, select its classes and staff, and press Save.
- Choose a head of section to see them on the section list.

Headcounts per section show on the same page."""},
]

TOURS = [
    {'key': 'dashboard-welcome', 'title': 'Welcome', 'path_prefix': '/dashboard/', 'audience': 'ADMIN',
     'steps': [{'title': 'Welcome to your school portal', 'text': 'Everything is in the menu on the left, grouped by what you do: academics, people, finance, welfare and more.'},
               {'title': 'Start with the basics', 'text': 'Add classes, subjects, staff and students first. The help centre has a step-by-step guide.'},
               {'title': 'Help is one click away', 'text': 'Open the Help Centre in the menu for short guides to every part of the portal.'}]},
    {'key': 'library-desk', 'title': 'Library desk', 'path_prefix': '/library/desk/', 'audience': 'STAFF',
     'steps': [{'title': 'Scan the card first', 'text': 'Scan the borrower\'s ID card or type an admission number or staff username.'},
               {'title': 'Then scan each book', 'text': 'The book is lent with the usual loan period. To return a book, scan it in the box on the right.'}]},
    {'key': 'transport-trip', 'title': 'Running a bus trip', 'path_prefix': '/transport/trips/', 'audience': 'STAFF',
     'steps': [{'title': 'Keep this page open', 'text': 'While it is open your phone shares the bus position with parents.'},
               {'title': 'Scan children on and off', 'text': 'Scan the ID card or tap On/Off. End the trip only after walking the bus.'}]},
    {'key': 'welfare-overview', 'title': 'Welfare', 'path_prefix': '/welfare/', 'audience': 'STAFF',
     'steps': [{'title': 'Health, conduct and care', 'text': 'Report incidents and accidents, find emergency contacts, and raise safeguarding concerns from here.'},
               {'title': 'Safeguarding', 'text': 'If you are worried about a child, record it straight away. The safeguarding lead is told at once.'}]},
    {'key': 'facilities-overview', 'title': 'Facilities', 'path_prefix': '/facilities/', 'audience': 'STAFF',
     'steps': [{'title': 'Report a fault', 'text': 'Anything broken? Report it with a photo and you will be emailed when it is fixed.'},
               {'title': 'Ask for items', 'text': 'Use Requisitions to ask the store for what you need.'}]},
]
