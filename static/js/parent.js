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