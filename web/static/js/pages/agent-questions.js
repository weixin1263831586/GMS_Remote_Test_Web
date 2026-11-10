// Model questions share one renderer across project tools and commands.
const agentQuestionValues = new Map();

function clearAgentQuestions() {
    agentQuestionValues.clear();
}

function renderAgentQuestion(message, session) {
    const fields = message.data?.question?.fields || [];
    const active = session.agent_question?.id === message.id && session.status !== 'thinking';
    const values = agentQuestionValues.get(message.id) || {};
    const controls = fields.map(field => {
        const name = String(field.name || '');
        const value = values[name] || '';
        const attributes = `data-agent-choice="${escapeHtml(name)}" data-change="updateAgentQuestionValue"
            data-a0="${escapeHtml(message.id)}" data-a1="${escapeHtml(name)}" data-r2="value" ${active ? '' : 'disabled'}`;
        const options = field.options || [];
        const control = options.length
            ? `<select ${attributes}><option value="">请选择${escapeHtml(field.label)}</option>${options.map(option =>
                `<option value="${escapeHtml(option.value)}" ${option.value === value ? 'selected' : ''}>${escapeHtml(option.label)}</option>`
            ).join('')}</select>`
            : `<input type="text" maxlength="2048" value="${escapeHtml(value)}" ${attributes}
                data-input="updateAgentQuestionValue" placeholder="请输入${escapeHtml(field.label)}">`;
        return `<label class="agent-question-field">${escapeHtml(field.label)}${field.required ? ' *' : ''}${control}</label>`;
    }).join('');
    return `<div class="agent-question" id="agent-question-${escapeHtml(message.id)}">${controls}
        <div class="agent-actions"><button class="btn-xs" data-click="submitAgentQuestion" data-a0="${escapeHtml(message.id)}"
            ${active ? '' : 'disabled'}>提交选择，继续规划</button></div></div>`;
}

function updateAgentQuestionValue(messageId, name, value) {
    const values = agentQuestionValues.get(messageId) || {};
    values[name] = value;
    agentQuestionValues.set(messageId, values);
}

async function submitAgentQuestion(messageId) {
    const form = document.getElementById(`agent-question-${messageId}`);
    if (!form) return;
    const values = {};
    form.querySelectorAll('[data-agent-choice]').forEach(control => {
        values[control.dataset.agentChoice] = control.value;
    });
    await sendAgentAction('agent_choices', JSON.stringify({question_id: messageId, values}), '提交资源选择');
}
