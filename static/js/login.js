// Map roles to theme files
const themes = {
    educator: '../static/css/themes/educator.css',
    parent: '../static/css/themes/parent.css',
    student: '../static/css/themes/student.css'
};

const icons = {
    educator: '<i class="ph ph-graduation-cap"></i>',
    parent: '<i class="ph ph-users"></i>',
    student: '<i class="ph ph-book-open"></i>'
};

const titles = {
    educator: 'Educator Login',
    parent: 'Parent Login',
    student: 'Student Login'
};

const redirectUrls = {
    educator: 'educator_dashboard.html',
    parent: 'parent_dashboard.html',
    student: 'student_dashboard.html'
};

let currentRole = null;

function selectRole(role) {
    currentRole = role;
    
    // Switch Theme
    document.getElementById('theme-style').href = themes[role];
    
    // Update Form Content
    document.getElementById('form-icon').innerHTML = icons[role];
    document.getElementById('form-icon').style.background = role === 'educator' ? '#EFF6FF' : role === 'parent' ? '#F0FDF4' : '#FAF5FF';
    document.getElementById('form-icon').style.color = role === 'educator' ? '#2563EB' : role === 'parent' ? '#16A34A' : '#9333EA';
    
    document.getElementById('form-title').innerText = titles[role];
    
    // Toggle Views
    document.getElementById('role-selection').classList.add('hidden');
    document.getElementById('login-form-container').style.display = 'block';
}

function resetSelection() {
    currentRole = null;
    document.getElementById('role-selection').classList.remove('hidden');
    document.getElementById('login-form-container').style.display = 'none';
}

function handleLogin(e) {
    e.preventDefault();
    if (currentRole && redirectUrls[currentRole]) {
        // In a real app, you would validate credentials here
        window.location.href = redirectUrls[currentRole];
    }
}