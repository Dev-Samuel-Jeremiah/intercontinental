document.addEventListener('DOMContentLoaded', () => {

    const sidebar = document.querySelector('.sidebar');

    if (sidebar) {
      
        sidebar.addEventListener('mouseenter', () => {
            sidebar.classList.remove('collapsed');
        });

    
        sidebar.addEventListener('mouseleave', () => {
            sidebar.classList.add('collapsed');
        });
    }

    const tabs = document.querySelectorAll('.sub-nav-link, .nav-pill'); 
    const tabContents = document.querySelectorAll('.tab-content');

   
    const dynamicBtn = document.querySelector('#dynamic-action-btn span');

    if (tabs.length > 0) {
        tabs.forEach(tab => {
            tab.addEventListener('click', (e) => {
                e.preventDefault();

                
                tabs.forEach(t => t.classList.remove('active'));
                
               
                tab.classList.add('active');

                
                tabContents.forEach(content => content.classList.remove('active'));

               
                const targetId = tab.getAttribute('data-tab');
                const targetContent = document.getElementById(targetId);
                if (targetContent) {
                    targetContent.classList.add('active');
                }

                
                if (dynamicBtn) {
                    const newText = tab.getAttribute('data-btn-text');
                    if(newText) dynamicBtn.textContent = newText;
                }
            });
        });
    }

   
    const currentPath = window.location.pathname.split("/").pop();
    const navItems = document.querySelectorAll('.nav-item');
    
    navItems.forEach(item => {
        const itemHref = item.getAttribute('href');
       
        if (itemHref === currentPath || (currentPath === '' && itemHref === 'index.html')) {
           
            navItems.forEach(nav => nav.classList.remove('active'));
         
            item.classList.add('active');
        }
    });
    
   
    const smallPills = document.querySelectorAll('.btn-pill-sm');
    if (smallPills.length > 0) {
        smallPills.forEach(pill => {
            pill.addEventListener('click', function() {
                
                const siblings = this.parentElement.querySelectorAll('.btn-pill-sm');
                siblings.forEach(s => s.classList.remove('active'));
                this.classList.add('active');
            });
        });
    }
});