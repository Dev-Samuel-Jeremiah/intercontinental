// Main Tab Switcher
function switchTab(tabName, btn) {
    // Hide all tab panes
    const panes = document.querySelectorAll('.tab-pane');
    panes.forEach(p => p.classList.add('d-none'));
    
    // Show selected pane
    document.getElementById('tab-' + tabName).classList.remove('d-none');
    
    // Update button states
    const buttons = document.querySelectorAll('.nav-btn');
    buttons.forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
}

// Sub Tab Switcher (Learning)
function switchSubTab(subTabName, btn) {
    const container = document.getElementById('tab-learning');
    
    // Hide all sub-sections specific to learning
    container.querySelector('#sub-assignments').classList.add('d-none');
    container.querySelector('#sub-notes').classList.add('d-none');
    container.querySelector('#sub-videos').classList.add('d-none');
    container.querySelector('#sub-discussions').classList.add('d-none');
    
    // Show selected sub-section
    container.querySelector('#sub-' + subTabName).classList.remove('d-none');
    
    // Update sub-nav button states
    const buttons = container.querySelectorAll('.sub-nav-btn');
    buttons.forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
}