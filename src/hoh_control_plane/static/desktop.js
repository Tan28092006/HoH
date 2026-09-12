/* Local desktop controls: no bearer credential is exposed to the browser. */
(() => {
  let session;
  const controls = document.createElement('div');
  controls.className = 'desktop-actions';
  controls.innerHTML = '<button id="new-project" class="secondary">Thêm dự án</button><button id="agent-prompts" class="secondary">Prompt cho agent</button><button id="shutdown-hoh" class="secondary desktop-danger">Tắt HoH</button>';
  document.querySelector('.head-actions').prepend(controls);
  const notice = document.createElement('p');
  notice.className = 'desktop-notice';
  notice.setAttribute('role', 'status');
  document.querySelector('.page-head').after(notice);

  function modal(title, content) {
    const dialog = document.createElement('dialog');
    dialog.className = 'desktop-dialog';
    const heading = document.createElement('h2');
    heading.textContent = title;
    dialog.append(heading);
    dialog.insertAdjacentHTML('beforeend', content);
    document.body.append(dialog);
    dialog.addEventListener('close', () => dialog.remove());
    dialog.querySelectorAll('[data-close]').forEach(button => button.addEventListener('click', () => dialog.close()));
    dialog.showModal();
    return dialog;
  }

  async function post(path, payload = {}) {
    const response = await fetch(path, {method: 'POST', headers: {'Content-Type': 'application/json', 'X-HoH-Local-Token': session.csrf_token}, body: JSON.stringify(payload)});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error?.message || data.detail || 'Không thực hiện được thao tác. Kiểm tra dữ liệu và thử lại.');
    return data;
  }

  async function loadSession() {
    const response = await fetch('/local/session', {cache: 'no-store'});
    if (!response.ok) throw new Error('Không đọc được trạng thái HoH local.');
    session = await response.json();
    const picker = document.querySelector('#project-picker');
    const selected = new URLSearchParams(location.search).get('project') || picker.value;
    picker.replaceChildren(...session.projects.map(project => new Option(project.name, project.project_id)));
    picker.value = selected;
    document.querySelector('#agent-prompts').disabled = !session.projects.some(project => project.project_id === selected);
    if (session.shutting_down) showDraining();
  }

  let draining = false;
  function showDraining() {
    if (draining) return;
    draining = true;
    controls.querySelectorAll('button').forEach(button => {button.disabled = true;});
    notice.textContent = 'Đang tắt HoH — chờ worker hiện tại kết thúc. Các chat đang dùng MCP sẽ mất kết nối.';
    if (typeof eventStream !== 'undefined' && eventStream) eventStream.close();
    const timer = setInterval(async () => {
      try {
        const response = await fetch('/local/session', {cache: 'no-store'});
        if (!response.ok) throw new Error();
        const status = await response.json();
        notice.textContent = `Đang tắt HoH — còn ${status.running_workers} worker. Chờ công việc hiện tại kết thúc.`;
      } catch {
        clearInterval(timer);
        document.querySelector('#connection-label').textContent = 'Đã tắt';
        notice.textContent = 'HoH đã ngắt kết nối sau yêu cầu tắt. Bấm icon HoH trên Desktop để mở lại.';
      }
    }, 1500);
  }

  document.querySelector('#new-project').addEventListener('click', () => {
    const dialog = modal('Thêm dự án', `<form id="new-project-form">
      <label>Tên dự án<input name="name" required maxlength="160" autocomplete="off"></label>
      <label>Project ID<input name="project_id" required pattern="[A-Za-z0-9._-]+" maxlength="80" placeholder="vi-du: cua-hang" autocomplete="off"></label>
      <p class="desktop-hint">ID cố định để các agent chọn đúng dự án. Dùng chữ, số, dấu gạch ngang hoặc gạch dưới.</p>
      <label>Mô tả dự án<textarea name="description" maxlength="4000" rows="3"></textarea></label>
      <label>Nhánh chính<input name="default_branch" required value="main" maxlength="200"></label>
      <p class="desktop-hint">Sau khi tạo, giao prompt cho Planner để cấu hình repository và lệnh test trước khi phát hành ticket.</p>
      <p role="alert" class="desktop-form-error"></p>
      <div class="desktop-dialog-actions"><button type="button" data-close class="secondary">Hủy</button><button type="submit" class="primary">Tạo dự án</button></div>
    </form>`);
    dialog.querySelector('form').addEventListener('submit', async event => {
      event.preventDefault();
      const submit = dialog.querySelector('[type=submit]');
      submit.disabled = true;
      try {
        const project = await post('/local/projects', Object.fromEntries(new FormData(event.target)));
        location.assign('/?project=' + encodeURIComponent(project.project_id));
      } catch (error) {
        dialog.querySelector('[role=alert]').textContent = error.message;
        submit.disabled = false;
      }
    });
  });

  document.querySelector('#agent-prompts').addEventListener('click', async () => {
    const project = document.querySelector('#project-picker').value;
    try {
      const response = await fetch('/local/prompts?project_id=' + encodeURIComponent(project));
      if (!response.ok) throw new Error('Không tải được prompt cho dự án này.');
      const data = await response.json();
      const dialog = modal('Prompt cho agent', `<p class="desktop-prompt-project"></p>
        <div class="desktop-role-tabs" role="group" aria-label="Vai trò">${['Planner','Developer','QA','Integrator'].map(role => `<button class="secondary" type="button" data-role="${role.toLowerCase()}">${role}</button>`).join('')}</div>
        <label>Prompt cài đặt<textarea id="agent-prompt-text" readonly rows="14" spellcheck="false"></textarea></label>
        <p class="desktop-hint">Dành cho session trên máy này. Mỗi vai trò/instance chỉ dùng cho một chat đang làm việc. Project trong prompt đã được gắn cố định.</p>
        <p class="desktop-copy-status" role="status"></p><div class="desktop-dialog-actions"><button class="secondary" data-close>Đóng</button><button id="copy-agent-prompt" class="primary">Sao chép prompt</button></div>`);
      dialog.querySelector('.desktop-prompt-project').textContent = 'Project ID: ' + project;
      const text = dialog.querySelector('textarea');
      function choose(role) {
        text.value = data.prompts[role];
        dialog.querySelectorAll('[data-role]').forEach(button => button.setAttribute('aria-pressed', String(button.dataset.role === role)));
      }
      dialog.querySelectorAll('[data-role]').forEach(button => button.addEventListener('click', () => choose(button.dataset.role)));
      choose('planner');
      dialog.querySelector('#copy-agent-prompt').addEventListener('click', async () => {
        try {
          await navigator.clipboard.writeText(text.value);
          dialog.querySelector('.desktop-copy-status').textContent = 'Đã sao chép. Dán vào session đúng vai trò.';
        } catch {
          text.focus(); text.select();
          dialog.querySelector('.desktop-copy-status').textContent = 'Browser chưa cho phép clipboard. Nhấn Ctrl+C để sao chép phần đã chọn.';
        }
      });
    } catch (error) { notice.textContent = error.message; }
  });

  document.querySelector('#shutdown-hoh').addEventListener('click', () => {
    const dialog = modal('Tắt HoH trên máy này?', `<p>HoH sẽ dừng khởi chạy job mới, chờ worker hiện tại kết thúc rồi tắt UI, MCP và các service nền. Các phiên agent đang kết nối sẽ mất kết nối. Mở lại bằng icon Desktop.</p>
      <p role="alert" class="desktop-form-error"></p><div class="desktop-dialog-actions"><button class="secondary" data-close>Tiếp tục sử dụng</button><button id="confirm-shutdown" class="primary desktop-danger">Xác nhận tắt</button></div>`);
    dialog.querySelector('#confirm-shutdown').addEventListener('click', async event => {
      event.target.disabled = true;
      try { await post('/local/shutdown'); dialog.close(); showDraining(); }
      catch (error) { dialog.querySelector('[role=alert]').textContent = error.message; event.target.disabled = false; }
    });
  });
  controls.querySelectorAll('button').forEach(button => {button.disabled = true;});
  loadSession().then(() => {if (!draining) controls.querySelectorAll('button').forEach(button => {
    button.disabled = button.id === 'agent-prompts' && !session.projects.some(project => project.project_id === document.querySelector('#project-picker').value);
  });})
    .catch(error => {notice.textContent = error.message;});
})();
