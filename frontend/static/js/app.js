// Global UI Elements and State
const elements = {
    spedPath: document.getElementById('spedPath'),
    entradaDir: document.getElementById('entradaDir'),
    cteDir: document.getElementById('cteDir'),
    spedCheck: document.getElementById('spedCheck'),
    entradaCheck: document.getElementById('entradaCheck'),
    cteCheck: document.getElementById('cteCheck'),
    companyNameLabel: document.getElementById('companyNameLabel'),
    loadingOverlay: document.getElementById('loadingOverlay'),
    configContent: document.getElementById('configContent'),
    configToggleIcon: document.getElementById('configToggleIcon'),
    
    // KPI Widgets
    kpiMissingXml: document.getElementById('kpiMissingXml'),
    kpiValueDisc: document.getElementById('kpiValueDisc'),
    kpiIcmsIssues: document.getElementById('kpiIcmsIssues'),
    kpiCteIssues: document.getElementById('kpiCteIssues'),
    kpiCfopDisc: document.getElementById('kpiCfopDisc'),
    kpiCteCfopDisc: document.getElementById('kpiCteCfopDisc'),
    kpiUnmapped: document.getElementById('kpiUnmapped'),
    
    // Badges in reports
    badgeMissingXml: document.getElementById('badgeMissingXml'),
    badgeValueDisc: document.getElementById('badgeValueDisc'),
    badgeIcmsIssues: document.getElementById('badgeIcmsIssues'),
    badgeIcmsSt: document.getElementById('badgeIcmsSt'),
    badgeCteIssues: document.getElementById('badgeCteIssues'),
    badgeCfopDisc: document.getElementById('badgeCfopDisc'),
    badgeCteCfopDisc: document.getElementById('badgeCteCfopDisc'),
    badgeNewProducts: document.getElementById('badgeNewProducts'),
    badgeNewCteCfops: document.getElementById('badgeNewCteCfops'),
    
    // Tables
    tableMissingXml: document.getElementById('tableMissingXml').querySelector('tbody'),
    tableValueDisc: document.getElementById('tableValueDisc').querySelector('tbody'),
    tableIcmsIssues: document.getElementById('tableIcmsIssues').querySelector('tbody'),
    tableIcmsSt: document.getElementById('tableIcmsSt').querySelector('tbody'),
    tableCteIssues: document.getElementById('tableCteIssues').querySelector('tbody'),
    tableCfopDisc: document.getElementById('tableCfopDisc').querySelector('tbody'),
    tableCteCfopDisc: document.getElementById('tableCteCfopDisc').querySelector('tbody'),
    tableNewProducts: document.getElementById('tableNewProducts').querySelector('tbody'),
    tableNewCteCfops: document.getElementById('tableNewCteCfops').querySelector('tbody'),
    tableIcmsRulesList: document.getElementById('tableIcmsRulesList').querySelector('tbody'),
    tableClassList: document.getElementById('tableClassList').querySelector('tbody'),
    tableCteClassList: document.getElementById('tableCteClassList').querySelector('tbody'),
    tableDifalInterno: document.getElementById('tableDifalInterno').querySelector('tbody'),
    tableDifalInterestadual: document.getElementById('tableDifalInterestadual').querySelector('tbody'),
    tableCteXmlReport: document.getElementById('tableCteXmlReport').querySelector('tbody'),
    tableNfeXmlReport: document.getElementById('tableNfeXmlReport').querySelector('tbody'),
    
    // Section IDs
    newProductsSection: document.getElementById('newProductsSection')
};

let currentAuditData = null;
let isConfigOpen = false;

// Global Data lists for dynamic filtering and email generation
let difalInternoData = [];
let difalInterestadualData = [];
let emailItemsMap = {};
let emailItemIdCounter = 0;

// ----------------- INITIALIZATION -----------------

document.addEventListener('DOMContentLoaded', () => {
    loadConfig();
    loadIcmsRules();
    loadClassifications();
    loadCteClassifications();
});

// Toggle Foldable Configuration Panel
function toggleConfig() {
    isConfigOpen = !isConfigOpen;
    if (isConfigOpen) {
        elements.configContent.style.display = 'block';
        elements.configToggleIcon.classList.replace('fa-chevron-down', 'fa-chevron-up');
    } else {
        elements.configContent.style.display = 'none';
        elements.configToggleIcon.classList.replace('fa-chevron-up', 'fa-chevron-down');
    }
}

// Switch tabs
function switchTab(tabId) {
    // Hide all tab contents
    document.querySelectorAll('.tab-content').forEach(content => {
        content.classList.remove('active');
    });
    
    // Remove active state from all tab buttons
    document.querySelectorAll('.tab-btn').forEach(btn => {
        btn.classList.remove('active');
    });
    
    // Show active tab
    const activeTab = document.getElementById(`tab-${tabId}`);
    if (activeTab) {
        activeTab.classList.add('active');
    }
    
    // Set button active
    const btn = Array.from(document.querySelectorAll('.tab-btn')).find(b => b.getAttribute('onclick').includes(tabId));
    if (btn) {
        btn.classList.add('active');
    }
}

// ----------------- CONFIG PATHS -----------------

async function loadConfig() {
    try {
        const response = await fetch('/api/config');
        const data = await response.json();
        
        elements.spedPath.value = data.sped_path;
        elements.entradaDir.value = data.entrada_dir;
        elements.cteDir.value = data.cte_dir;
        
        updateCheckIcon(elements.spedCheck, data.sped_exists);
        updateCheckIcon(elements.entradaCheck, data.entrada_exists);
        updateCheckIcon(elements.cteCheck, data.cte_exists);
        
        // Open configuration panel on start if any path is missing
        if (!data.sped_exists || !data.entrada_exists || !data.cte_exists) {
            isConfigOpen = false;
            toggleConfig();
        }
    } catch (error) {
        console.error('Error loading config:', error);
    }
}

async function savePaths() {
    showLoading(true, "Validando caminhos...");
    try {
        const response = await fetch('/api/config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                sped_path: elements.spedPath.value,
                entrada_dir: elements.entradaDir.value,
                cte_dir: elements.cteDir.value
            })
        });
        
        if (response.ok) {
            const data = await response.json();
            updateCheckIcon(elements.spedCheck, data.sped_exists);
            updateCheckIcon(elements.entradaCheck, data.entrada_exists);
            updateCheckIcon(elements.cteCheck, data.cte_exists);
            
            if (data.sped_exists && data.entrada_exists && data.cte_exists) {
                alert('Caminhos salvos e validados com sucesso!');
                toggleConfig(); // Collapse if valid
            } else {
                alert('Atenção: Um ou mais caminhos fornecidos não são válidos. Verifique as indicações com ícones verdes/vermelhos.');
            }
        } else {
            const errData = await response.json().catch(() => ({}));
            alert('Erro ao salvar caminhos: ' + (errData.message || 'Erro interno do servidor.'));
        }
    } catch (error) {
        console.error(error);
        alert('Erro ao conectar ao servidor. Certifique-se de que o comando "python server.py" ainda está rodando no prompt.');
    } finally {
        showLoading(false);
    }
}

async function browseForPath(inputId, browseType) {
    const inputEl = document.getElementById(inputId);
    const currentVal = inputEl.value;
    
    showLoading(true, "Aguardando seleção de pasta/arquivo na sua tela...");
    try {
        const response = await fetch(`/api/browse?type=${browseType}&current=${encodeURIComponent(currentVal)}`);
        const data = await response.json();
        
        if (response.ok && data.status === 'success' && data.path) {
            inputEl.value = data.path;
            savePaths(); // Instancia a validação automática!
        }
    } catch (error) {
        console.error(error);
        alert('Erro ao abrir buscador de arquivos local.');
    } finally {
        showLoading(false);
    }
}

function populateBulkClassificationSelectors(unmappedItems) {
    const cnpjSelect = document.getElementById('bulkCnpjSelect');
    const cfopSelect = document.getElementById('bulkCfopSelect');
    if (!cnpjSelect || !cfopSelect) return;
    
    const lastCnpj = cnpjSelect.value;
    const lastCfop = cfopSelect.value;
    
    cnpjSelect.innerHTML = '<option value="">Todos os Fornecedores</option>';
    cfopSelect.innerHTML = '<option value="">Todos os CFOPs</option>';
    
    const cnpjs = [...new Set(unmappedItems.map(item => item.supplier_cnpj))].sort();
    const cfops = [...new Set(unmappedItems.map(item => item.supplier_cfop))].sort();
    
    cnpjs.forEach(cnpj => {
        const opt = document.createElement('option');
        opt.value = cnpj;
        opt.textContent = cnpj;
        cnpjSelect.appendChild(opt);
    });
    
    cfops.forEach(cfop => {
        const opt = document.createElement('option');
        opt.value = cfop;
        opt.textContent = cfop;
        cfopSelect.appendChild(opt);
    });
    
    cnpjSelect.value = lastCnpj;
    cfopSelect.value = lastCfop;
}

function filterNewProducts() {
    const cnpj = document.getElementById('bulkCnpjSelect').value;
    const cfop = document.getElementById('bulkCfopSelect').value;
    
    const rows = elements.tableNewProducts.querySelectorAll('tr');
    rows.forEach(tr => {
        if (tr.id && tr.id.startsWith('unmapped_row_')) {
            const index = parseInt(tr.id.replace('unmapped_row_', ''));
            const item = currentAuditData.unmapped_items[index];
            if (!item) return;
            
            const matchCnpj = !cnpj || item.supplier_cnpj === cnpj;
            const matchCfop = !cfop || item.supplier_cfop === cfop;
            
            if (matchCnpj && matchCfop) {
                tr.style.display = '';
            } else {
                tr.style.display = 'none';
            }
        }
    });
}

async function applyBulkClassification() {
    const cnpj = document.getElementById('bulkCnpjSelect').value;
    const cfop = document.getElementById('bulkCfopSelect').value;
    const entryCfop = document.getElementById('bulkEntryCfop').value;
    
    if (!cnpj || !cfop) {
        alert("Por favor, selecione o CNPJ Fornecedor e o CFOP Fornecedor para a classificação em massa.");
        return;
    }
    if (!entryCfop || entryCfop.length !== 4) {
        alert("Forneça um CFOP de entrada válido de 4 dígitos!");
        return;
    }
    
    const matchingItems = currentAuditData.unmapped_items.filter(item => {
        return item.supplier_cnpj === cnpj && item.supplier_cfop === cfop;
    });
    
    if (matchingItems.length === 0) {
        alert("Nenhum item pendente atende aos filtros selecionados.");
        return;
    }
    
    if (!confirm(`Deseja classificar todos os ${matchingItems.length} itens do fornecedor ${cnpj} com o CFOP de entrada ${entryCfop}?`)) {
        return;
    }
    
    const companyCnpj = (currentAuditData && currentAuditData.company_cnpj) || document.getElementById('classCnpj').value || "13639387000172";
    
    showLoading(true, "Gravando classificações em massa...");
    try {
        const response = await fetch('/api/rules/classification/bulk', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                company_cnpj: companyCnpj,
                supplier_cnpj: cnpj,
                supplier_cfop: cfop,
                entry_cfop: entryCfop,
                items: matchingItems.map(item => ({
                    supplier_cnpj: item.supplier_cnpj || cnpj,
                    supplier_cfop: item.supplier_cfop || cfop,
                    product_code: item.product_code,
                    product_description: item.product_description
                }))
            })
        });
        
        if (response.ok) {
            alert(`${matchingItems.length} itens foram classificados com sucesso!`);
            
            currentAuditData.unmapped_items = currentAuditData.unmapped_items.filter(item => {
                return !matchingItems.includes(item);
            });
            
            // Re-render table and update badges
            renderNewProductsTable(currentAuditData.unmapped_items);
            
            // Apply current filters so they persist on screen
            filterNewProducts();
            
            // Adjust KPI count
            elements.kpiUnmapped.textContent = currentAuditData.unmapped_items.length + currentAuditData.unmapped_cte_items.length;
            elements.badgeNewProducts.textContent = currentAuditData.unmapped_items.length;
            
            document.getElementById('bulkEntryCfop').value = '';
            
            loadClassifications();
        } else {
            const err = await response.json();
            alert('Erro: ' + err.error);
        }
    } catch (error) {
        console.error(error);
        alert('Erro de conexão ao gravar classificações em massa.');
    } finally {
        showLoading(false);
    }
}

function updateCheckIcon(wrapperEl, isValid) {
    if (isValid) {
        wrapperEl.className = "check-icon valid";
        wrapperEl.innerHTML = '<i class="fa-solid fa-circle-check"></i>';
    } else {
        wrapperEl.className = "check-icon invalid";
        wrapperEl.innerHTML = '<i class="fa-solid fa-circle-xmark"></i>';
    }
}

// ----------------- AUDIT RUNNER & RENDER -----------------

async function runAudit() {
    showLoading(true, "Executando auditorias tributárias...");
    try {
        const response = await fetch('/api/audit');
        const data = await response.json();
        
        if (data.status === 'success') {
            currentAuditData = data;
            renderAuditResults(data);
            switchTab('auditResults');
        } else {
            alert('Erro na auditoria: ' + data.message);
        }
    } catch (error) {
        alert('Erro ao processar a auditoria.');
        console.error(error);
    } finally {
        showLoading(false);
    }
}


function renderNewProductsTable(unmappedItems) {
    elements.tableNewProducts.innerHTML = '';
    if (!unmappedItems || unmappedItems.length === 0) {
        elements.tableNewProducts.innerHTML = '<tr><td colspan="7" class="text-center text-success"><i class="fa-solid fa-circle-check"></i> Todos os produtos já estão classificados!</td></tr>';
        elements.newProductsSection.style.display = 'none';
        
        // Clear selectors since no items are unmapped
        const cnpjSelect = document.getElementById('bulkCnpjSelect');
        const cfopSelect = document.getElementById('bulkCfopSelect');
        if (cnpjSelect) cnpjSelect.innerHTML = '<option value="">Todos os Fornecedores</option>';
        if (cfopSelect) cfopSelect.innerHTML = '<option value="">Todos os CFOPs</option>';
    } else {
        elements.newProductsSection.style.display = 'block';
        unmappedItems.forEach((item, index) => {
            const inputId = `new_input_cfop_${index}`;
            let suggestedCfop = "";
            const scfop = item.supplier_cfop;
            if (scfop.startsWith('5')) {
                suggestedCfop = '1' + scfop.substring(1);
            } else if (scfop.startsWith('6')) {
                suggestedCfop = '2' + scfop.substring(1);
            }
            
            const tr = document.createElement('tr');
            tr.id = `unmapped_row_${index}`;
            tr.innerHTML = `
                <td><span class="badge badge-secondary">${item.supplier_cfop}</span></td>
                <td data-t="s"><code>${item.supplier_cnpj}</code></td>
                <td><code>${item.product_code}</code></td>
                <td>${item.product_description}</td>
                <td><span class="text-success" style="cursor:pointer;" onclick="document.getElementById('${inputId}').value='${suggestedCfop}'">Sugerido: ${suggestedCfop}</span></td>
                <td>
                    <input type="text" id="${inputId}" value="${suggestedCfop}" maxlength="4" placeholder="Ex: 1102">
                </td>
                <td class="text-center">
                    <button class="btn btn-primary" style="padding: 4px 10px; font-size: 11px;" onclick="saveNewItemClassification(${index}, '${inputId}')">
                        <i class="fa-solid fa-plus"></i> Gravar Regra
                    </button>
                </td>
            `;
            elements.tableNewProducts.appendChild(tr);
        });
        populateBulkClassificationSelectors(unmappedItems);
    }
}

function renderAuditResults(data) {
    // Reset email map
    emailItemsMap = {};
    emailItemIdCounter = 0;
    document.getElementById('emailDraftContainer').style.display = 'none';

    // Update company badge
    elements.companyNameLabel.textContent = `${data.company_name} | CNPJ: ${data.company_cnpj}`;
    document.getElementById('classCnpj').value = data.company_cnpj;
    document.getElementById('cteClassCnpj').value = data.company_cnpj;
    
    // 1. Update KPI widgets
    elements.kpiMissingXml.textContent = data.missing_xmls.length;
    elements.kpiValueDisc.textContent = data.value_discrepancies.length;
    elements.kpiIcmsIssues.textContent = data.icms_issues.length;
    elements.kpiCteIssues.textContent = data.cte_tomador_issues.length;
    elements.kpiCfopDisc.textContent = data.cfop_classifications_issues.length;
    elements.kpiCteCfopDisc.textContent = data.cte_cfop_classifications_issues.length;
    
    const unmappedTotal = data.unmapped_items.length + data.unmapped_cte_items.length;
    elements.kpiUnmapped.textContent = unmappedTotal;
    
    // 2. Update Badges
    elements.badgeMissingXml.textContent = data.missing_xmls.length;
    elements.badgeValueDisc.textContent = data.value_discrepancies.length;
    elements.badgeIcmsIssues.textContent = data.icms_issues.length;
    elements.badgeIcmsSt.textContent = data.icms_st_notes.length;
    elements.badgeCteIssues.textContent = data.cte_tomador_issues.length;
    elements.badgeCfopDisc.textContent = data.cfop_classifications_issues.length;
    elements.badgeCteCfopDisc.textContent = data.cte_cfop_classifications_issues.length;
    
    elements.badgeNewProducts.textContent = data.unmapped_items.length;
    if (elements.badgeNewCteCfops) {
        elements.badgeNewCteCfops.textContent = data.unmapped_cte_items.length;
    }
    
    // 3. Render Table 1 - Missing XMLs
    elements.tableMissingXml.innerHTML = '';
    if (data.missing_xmls.length === 0) {
        elements.tableMissingXml.innerHTML = '<tr><td colspan="7" class="text-center text-success"><i class="fa-solid fa-circle-check"></i> Todos os XMLs de entrada estão presentes!</td></tr>';
    } else {
        data.missing_xmls.forEach(item => {
            const tr = document.createElement('tr');
            const emailId = 'email_item_' + (emailItemIdCounter++);
            emailItemsMap[emailId] = {
                type: item.type === 'NF-e' ? 'NFe' : 'CTe',
                num: item.num,
                emit: item.emit,
                descr: item.type === 'NF-e' ? 'XML de NF-e faltante' : 'XML de CT-e faltante',
                details: 'Arquivo XML correspondente não localizado na pasta do período.'
            };
            tr.innerHTML = `
                <td class="text-center"><input type="checkbox" class="email-select-cb" value="${emailId}"></td>
                <td><span class="badge ${item.type === 'NF-e' ? 'badge-blue' : 'badge-purple'}">${item.type}</span></td>
                <td><strong>${item.num}</strong></td>
                <td><span class="badge badge-secondary">${item.ind_emit}</span> ${item.emit}</td>
                <td data-t="n" data-v="${item.vl_doc}">R$ ${item.vl_doc.toLocaleString('pt-BR', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</td>
                <td data-t="s"><code>${item.key}</code></td>
                <td class="text-danger"><i class="fa-solid fa-circle-xmark"></i> XML Faltante na Pasta</td>
            `;
            elements.tableMissingXml.appendChild(tr);
        });
    }
    
    // 4. Render Table 2 - Value Discrepancies
    elements.tableValueDisc.innerHTML = '';
    if (data.value_discrepancies.length === 0) {
        elements.tableValueDisc.innerHTML = '<tr><td colspan="7" class="text-center text-success"><i class="fa-solid fa-circle-check"></i> Sem divergências de valor contábil!</td></tr>';
    } else {
        data.value_discrepancies.forEach(item => {
            const tr = document.createElement('tr');
            const emailId = 'email_item_' + (emailItemIdCounter++);
            emailItemsMap[emailId] = {
                type: item.type === 'NF-e' ? 'NFe' : 'CTe',
                num: item.num,
                emit: item.emit_name,
                descr: 'Divergência de valor total do documento',
                details: `Valor contábil lançado no SPED (R$ ${item.sped_val.toFixed(2)}) diverge do XML (R$ ${item.xml_val.toFixed(2)}).`
            };
            tr.innerHTML = `
                <td class="text-center"><input type="checkbox" class="email-select-cb" value="${emailId}"></td>
                <td><span class="badge ${item.type === 'NF-e' ? 'badge-blue' : 'badge-purple'}">${item.type}</span></td>
                <td><strong>${item.num}</strong></td>
                <td data-t="n" data-v="${item.sped_val}">R$ ${item.sped_val.toLocaleString('pt-BR', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</td>
                <td data-t="n" data-v="${item.xml_val}">R$ ${item.xml_val.toLocaleString('pt-BR', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</td>
                <td class="text-danger" data-t="n" data-v="${item.diff}">R$ ${item.diff.toLocaleString('pt-BR', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</td>
                <td data-t="s"><code>${item.key}</code></td>
            `;
            elements.tableValueDisc.appendChild(tr);
        });
    }
    
    // 5. Render Table 3 - ICMS Credit Compliance Issues
    elements.tableIcmsIssues.innerHTML = '';
    if (data.icms_issues.length === 0) {
        elements.tableIcmsIssues.innerHTML = '<tr><td colspan="8" class="text-center text-success"><i class="fa-solid fa-circle-check"></i> Todos os créditos de ICMS estão devidamente lançados!</td></tr>';
    } else {
        data.icms_issues.forEach(item => {
            const tr = document.createElement('tr');
            const emailId = 'email_item_' + (emailItemIdCounter++);
            emailItemsMap[emailId] = {
                type: 'NFe',
                num: item.num,
                emit: item.emit_name,
                descr: item.descr,
                details: item.reason
            };
            tr.innerHTML = `
                <td class="text-center"><input type="checkbox" class="email-select-cb" value="${emailId}"></td>
                <td><strong>${item.num}</strong></td>
                <td>Item ${item.num_item}</td>
                <td>${item.descr}</td>
                <td><span class="badge badge-blue">${item.cfop}</span></td>
                <td class="text-danger" data-t="n" data-v="${item.sped_icms}">R$ ${item.sped_icms.toLocaleString('pt-BR', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</td>
                <td data-t="n" data-v="${item.xml_icms}">R$ ${item.xml_icms.toLocaleString('pt-BR', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</td>
                <td class="text-danger"><strong>${item.reason}</strong></td>
            `;
            elements.tableIcmsIssues.appendChild(tr);
        });
    }
    
    // 6. Render Table 4 - ICMS ST Notes (Apoio, non-selectable for email)
    elements.tableIcmsSt.innerHTML = '';
    if (data.icms_st_notes.length === 0) {
        elements.tableIcmsSt.innerHTML = '<tr><td colspan="6" class="text-center text-muted">Nenhum documento com destaque de ICMS ST identificado nos XMLs.</td></tr>';
    } else {
        data.icms_st_notes.forEach(note => {
            const itemsList = note.items.map(i => `<div>- Item ${i.num_item}: ${i.descr} (ST: R$ ${i.vl_icms_st.toFixed(2)})</div>`).join('');
            const tr = document.createElement('tr');
            tr.innerHTML = `
                <td class="text-center"><i class="fa-solid fa-receipt text-muted"></i></td>
                <td><strong>${note.num}</strong></td>
                <td>${note.emit_name}</td>
                <td class="text-danger" data-t="n" data-v="${note.vst_total}">R$ ${note.vst_total.toLocaleString('pt-BR', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</td>
                <td>
                    <div class="st-details">
                        <strong>Itens com imposto destacado:</strong>
                        ${itemsList}
                    </div>
                </td>
                <td data-t="s"><code>${note.key}</code></td>
            `;
            elements.tableIcmsSt.appendChild(tr);
        });
    }
    
    // 7. Render Table 5 - CT-e Tomador Issues
    elements.tableCteIssues.innerHTML = '';
    if (data.cte_tomador_issues.length === 0) {
        elements.tableCteIssues.innerHTML = '<tr><td colspan="7" class="text-center text-success"><i class="fa-solid fa-circle-check"></i> Todos os CT-es possuem a empresa como tomadora do serviço!</td></tr>';
    } else {
        data.cte_tomador_issues.forEach(item => {
            const tr = document.createElement('tr');
            const emailId = 'email_item_' + (emailItemIdCounter++);
            emailItemsMap[emailId] = {
                type: 'CTe',
                num: item.num,
                emit: item.emit_name,
                details: `Tomador indicado no XML (${item.tomador_id} - papel ${item.tomador_role}) diverge do CNPJ da empresa do SPED. Não deve ser lançado no SPED.`
            };
            tr.innerHTML = `
                <td class="text-center"><input type="checkbox" class="email-select-cb" value="${emailId}"></td>
                <td><strong>${item.num}</strong></td>
                <td>${item.emit_name}</td>
                <td class="text-danger" data-t="s"><code>${item.tomador_id}</code></td>
                <td><span class="badge badge-purple">${item.tomador_role}</span></td>
                <td data-t="s"><code>${item.company_cnpj}</code></td>
                <td class="text-danger"><strong>Tomador diverge da empresa do SPED! Não deve ser lançado.</strong></td>
            `;
            elements.tableCteIssues.appendChild(tr);
        });
    }
    
    // 8. Render Table 6 - CFOP Discrepancy List
    elements.tableCfopDisc.innerHTML = '';
    if (data.cfop_classifications_issues.length === 0) {
        elements.tableCfopDisc.innerHTML = '<tr><td colspan="8" class="text-center text-success"><i class="fa-solid fa-circle-check"></i> Sem divergências de CFOP para os produtos mapeados!</td></tr>';
    } else {
        data.cfop_classifications_issues.forEach(item => {
            const tr = document.createElement('tr');
            const emailId = 'email_item_' + (emailItemIdCounter++);
            emailItemsMap[emailId] = {
                type: 'NFe',
                num: item.num,
                emit: item.emit_name,
                descr: item.product_description,
                details: `CFOP de entrada incorreto no SPED: lançado CFOP ${item.sped_cfop}, correto deve ser CFOP ${item.expected_cfop}`
            };
            tr.innerHTML = `
                <td class="text-center"><input type="checkbox" class="email-select-cb" value="${emailId}"></td>
                <td><strong>${item.num}</strong></td>
                <td>Item ${item.num_item}</td>
                <td><code>${item.product_code}</code></td>
                <td>${item.product_description}</td>
                <td><span class="badge badge-secondary">${item.supplier_cfop}</span></td>
                <td class="text-danger">${item.sped_cfop}</td>
                <td class="text-success"><strong>${item.expected_cfop}</strong></td>
            `;
            elements.tableCfopDisc.appendChild(tr);
        });
    }
    
    // 8.5. Render Table 6.5 - CT-e CFOP Discrepancy List
    elements.tableCteCfopDisc.innerHTML = '';
    if (data.cte_cfop_classifications_issues.length === 0) {
        elements.tableCteCfopDisc.innerHTML = '<tr><td colspan="8" class="text-center text-success"><i class="fa-solid fa-circle-check"></i> Sem divergências de CFOP para os CT-es mapeados!</td></tr>';
    } else {
        data.cte_cfop_classifications_issues.forEach(item => {
            const tr = document.createElement('tr');
            const emailId = 'email_item_' + (emailItemIdCounter++);
            emailItemsMap[emailId] = {
                type: 'CTe',
                num: item.num,
                emit: item.emit_name,
                details: `CFOP de entrada incorreto no SPED: lançado CFOP ${item.sped_cfop}, correto deve ser CFOP ${item.expected_cfop}`
            };
            tr.innerHTML = `
                <td class="text-center"><input type="checkbox" class="email-select-cb" value="${emailId}"></td>
                <td><strong>${item.num}</strong></td>
                <td data-t="s"><code>${item.key}</code></td>
                <td>${item.emit_name}</td>
                <td><span class="badge badge-purple">${item.emit_uf}</span></td>
                <td><span class="badge badge-secondary">${item.supplier_cfop}</span></td>
                <td class="text-danger">${item.sped_cfop}</td>
                <td class="text-success"><strong>${item.expected_cfop}</strong></td>
            `;
            elements.tableCteCfopDisc.appendChild(tr);
        });
    }

    // Render warning for unmapped ICMS CFOPs
    const warningEl = document.getElementById('unmappedCfopsWarning');
    const warningTextEl = document.getElementById('unmappedCfopsWarningText');
    const warningButtonsEl = document.getElementById('unmappedCfopsWarningButtons');

    if (data.unmapped_icms_cfops && data.unmapped_icms_cfops.length > 0) {
        warningEl.style.display = 'block';
        warningTextEl.innerHTML = `Os seguintes CFOPs foram identificados no SPED, mas não possuem regras de ICMS cadastradas: <strong>${data.unmapped_icms_cfops.join(', ')}</strong>.`;
        
        warningButtonsEl.innerHTML = '';
        data.unmapped_icms_cfops.forEach(cfop => {
            const btn = document.createElement('button');
            btn.className = 'btn btn-warning';
            btn.style.padding = '6px 12px';
            btn.style.fontSize = '12px';
            btn.style.border = 'none';
            btn.style.borderRadius = 'var(--radius-sm)';
            btn.style.cursor = 'pointer';
            btn.innerHTML = `<i class="fa-solid fa-plus"></i> Cadastrar Regra ${cfop}`;
            btn.onclick = () => prefillIcmsRule(cfop);
            warningButtonsEl.appendChild(btn);
        });
    } else {
        warningEl.style.display = 'none';
    }

    function prefillIcmsRule(cfop) {
        switchTab('icmsRules');
        const ruleCfopInput = document.getElementById('ruleCfop');
        if (ruleCfopInput) {
            ruleCfopInput.value = cfop;
            ruleCfopInput.focus();
        }
    }
    
    // 9. Render Table 7 - Unmapped items (New products to map CFOP)
    renderNewProductsTable(data.unmapped_items || []);
    
    // Render unmapped CT-es
    elements.tableNewCteCfops.innerHTML = '';
    const newCteSection = document.getElementById('newCteCfopsSection');
    if (data.unmapped_cte_items.length === 0) {
        elements.tableNewCteCfops.innerHTML = '<tr><td colspan="6" class="text-center text-success"><i class="fa-solid fa-circle-check"></i> Todos os CT-es já estão classificados!</td></tr>';
        if (newCteSection) newCteSection.style.display = 'none';
    } else {
        if (newCteSection) newCteSection.style.display = 'block';
        data.unmapped_cte_items.forEach((item, index) => {
            const inputId = `new_cte_input_cfop_${index}`;
            let suggestedCfop = "";
            const scfop = item.supplier_cfop;
            if (scfop.startsWith('5')) {
                suggestedCfop = '1' + scfop.substring(1);
            } else if (scfop.startsWith('6')) {
                suggestedCfop = '2' + scfop.substring(1);
            }
            
            const tr = document.createElement('tr');
            tr.id = `unmapped_cte_row_${index}`;
            tr.innerHTML = `
                <td><span class="badge badge-secondary">${item.supplier_cfop}</span></td>
                <td><span class="badge badge-purple">${item.emitter_uf}</span></td>
                <td><span class="badge badge-blue"><strong>${item.current_sped_cfop}</strong></span></td>
                <td><span class="text-success" style="cursor:pointer;" onclick="document.getElementById('${inputId}').value='${suggestedCfop}'">Sugerido: ${suggestedCfop}</span></td>
                <td>
                    <input type="text" id="${inputId}" value="${suggestedCfop}" maxlength="4" placeholder="Ex: 1352">
                </td>
                <td class="text-center">
                    <button class="btn btn-primary" style="padding: 4px 10px; font-size: 11px;" onclick="saveNewCteClassification(${index}, '${inputId}')">
                        <i class="fa-solid fa-plus"></i> Gravar Regra
                    </button>
                </td>
            `;
            elements.tableNewCteCfops.appendChild(tr);
        });
    }
    
    // 10. Render DIFAL tables
    difalInternoData = data.difal_interno || [];
    renderDifalInternoTable(difalInternoData);
    
    difalInterestadualData = data.difal_interestadual || [];
    renderDifalInterestadualTable(difalInterestadualData);
    
    // 11. Render CT-e XML General Report
    renderCteXmlReportTable(data.cte_xml_list || []);
    
    // 12. Render NF-e XML General Report
    renderNfeXmlReportTable(data.nfe_xml_list || []);
    
    // Clear filter input on fresh run
    document.getElementById('filterEffPct').value = '';
}

function renderDifalInternoTable(rows) {
    elements.tableDifalInterno.innerHTML = '';
    if (rows.length === 0) {
        elements.tableDifalInterno.innerHTML = '<tr><td colspan="12" class="text-center text-muted">Nenhum item com CFOP 1556, 1551 ou 1910 cadastrado ou detectado nesta auditoria.</td></tr>';
        return;
    }
    rows.forEach(item => {
        const tr = document.createElement('tr');
        tr.innerHTML = `
            <td data-t="s"><code>${item.emit_cnpj}</code></td>
            <td><strong>${item.num_nf}</strong></td>
            <td>${item.descr}</td>
            <td data-t="s"><code>${item.ncm}</code></td>
            <td data-t="s"><code>${item.cest || '-'}</code></td>
            <td data-t="n" data-v="${item.vl_item}">R$ ${item.vl_item.toLocaleString('pt-BR', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</td>
            <td data-t="n" data-v="${item.vl_bc}">R$ ${item.vl_bc.toLocaleString('pt-BR', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</td>
            <td>${item.p_icms.toFixed(1)}%</td>
            <td data-t="n" data-v="${item.vl_icms}">R$ ${item.vl_icms.toLocaleString('pt-BR', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</td>
            <td data-t="s"><code>${item.c_benef || '-'}</code></td>
            <td><span class="badge badge-blue">${item.cfop_sped}</span></td>
            <td><span class="badge badge-purple">${item.eff_pct.toFixed(2)}%</span></td>
        `;
        elements.tableDifalInterno.appendChild(tr);
    });
}

function renderDifalInterestadualTable(rows) {
    elements.tableDifalInterestadual.innerHTML = '';
    if (rows.length === 0) {
        elements.tableDifalInterestadual.innerHTML = '<tr><td colspan="12" class="text-center text-muted">Nenhum item com CFOP 2556, 2910, 2551 ou 2949 cadastrado ou detectado nesta auditoria.</td></tr>';
        return;
    }
    rows.forEach(item => {
        const tr = document.createElement('tr');
        tr.innerHTML = `
            <td data-t="s"><code>${item.emit_cnpj}</code></td>
            <td><span class="badge badge-purple">${item.emit_uf}</span></td>
            <td><strong>${item.num_nf}</strong></td>
            <td>${item.descr}</td>
            <td data-t="s"><code>${item.ncm}</code></td>
            <td data-t="s"><code>${item.cest || '-'}</code></td>
            <td data-t="n" data-v="${item.vl_item}">R$ ${item.vl_item.toLocaleString('pt-BR', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</td>
            <td data-t="n" data-v="${item.vl_bc}">R$ ${item.vl_bc.toLocaleString('pt-BR', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</td>
            <td>${item.p_icms.toFixed(1)}%</td>
            <td data-t="n" data-v="${item.vl_icms}">R$ ${item.vl_icms.toLocaleString('pt-BR', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</td>
            <td data-t="n" data-v="${item.vl_icms_st}">R$ ${item.vl_icms_st.toLocaleString('pt-BR', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</td>
            <td><span class="badge badge-blue">${item.cfop_sped}</span></td>
        `;
        elements.tableDifalInterestadual.appendChild(tr);
    });
}

function renderCteXmlReportTable(rows) {
    elements.tableCteXmlReport.innerHTML = '';
    if (rows.length === 0) {
        elements.tableCteXmlReport.innerHTML = '<tr><td colspan="12" class="text-center text-muted">Nenhum CT-e identificado nas pastas.</td></tr>';
        return;
    }
    rows.forEach(item => {
        const tr = document.createElement('tr');
        tr.innerHTML = `
            <td><strong>${item.num}</strong></td>
            <td data-t="s"><code>${item.emit_cnpj}</code></td>
            <td><span class="badge badge-purple">${item.emit_uf}</span></td>
            <td>${item.rem_name}</td>
            <td>${item.dest_name}</td>
            <td>${item.toma_name}</td>
            <td data-t="s"><code>${item.nfe_nums}</code></td>
            <td data-t="s"><span style="font-size:10px; font-family:monospace; word-break:break-all;">${item.nfe_keys}</span></td>
            <td><span class="badge badge-blue">${item.nfe_cfop_sped}</span></td>
            <td data-t="n" data-v="${item.vl_doc}">R$ ${item.vl_doc.toLocaleString('pt-BR', {minimumFractionDigits:2, maximumFractionDigits:2})}</td>
            <td data-t="n" data-v="${item.vl_bc}">R$ ${item.vl_bc.toLocaleString('pt-BR', {minimumFractionDigits:2, maximumFractionDigits:2})}</td>
            <td data-t="n" data-v="${item.vl_icms}">R$ ${item.vl_icms.toLocaleString('pt-BR', {minimumFractionDigits:2, maximumFractionDigits:2})}</td>
        `;
        elements.tableCteXmlReport.appendChild(tr);
    });
}

function renderNfeXmlReportTable(rows) {
    elements.tableNfeXmlReport.innerHTML = '';
    if (rows.length === 0) {
        elements.tableNfeXmlReport.innerHTML = '<tr><td colspan="13" class="text-center text-muted">Nenhuma NF-e identificada nas pastas.</td></tr>';
        return;
    }
    rows.forEach(item => {
        const tr = document.createElement('tr');
        tr.innerHTML = `
            <td><strong>${item.num_nf}</strong></td>
            <td data-t="s"><code>${item.emit_cnpj}</code></td>
            <td>${item.emit_name}</td>
            <td>${item.descr}</td>
            <td><span class="badge badge-secondary">${item.cfop_xml}</span></td>
            <td><span class="badge badge-blue">${item.cfop_sped}</span></td>
            <td data-t="n" data-v="${item.vl_item}">R$ ${item.vl_item.toLocaleString('pt-BR', {minimumFractionDigits:2, maximumFractionDigits:2})}</td>
            <td data-t="n" data-v="${item.vl_bc}">R$ ${item.vl_bc.toLocaleString('pt-BR', {minimumFractionDigits:2, maximumFractionDigits:2})}</td>
            <td data-t="n" data-v="${item.vl_icms}">R$ ${item.vl_icms.toLocaleString('pt-BR', {minimumFractionDigits:2, maximumFractionDigits:2})}</td>
            <td data-t="n" data-v="${item.vl_icms_st}">R$ ${item.vl_icms_st.toLocaleString('pt-BR', {minimumFractionDigits:2, maximumFractionDigits:2})}</td>
            <td data-t="n" data-v="${item.fcp_st}">R$ ${item.fcp_st.toLocaleString('pt-BR', {minimumFractionDigits:2, maximumFractionDigits:2})}</td>
            <td data-t="n" data-v="${item.v_ipi}">R$ ${item.v_ipi.toLocaleString('pt-BR', {minimumFractionDigits:2, maximumFractionDigits:2})}</td>
            <td data-t="n" data-v="${item.v_ipi_devol}">R$ ${item.v_ipi_devol.toLocaleString('pt-BR', {minimumFractionDigits:2, maximumFractionDigits:2})}</td>
        `;
        elements.tableNfeXmlReport.appendChild(tr);
    });
}

function applyDifalInternoFilter() {
    const minVal = parseFloat(document.getElementById('filterEffPct').value);
    if (isNaN(minVal)) {
        renderDifalInternoTable(difalInternoData);
    } else {
        const filtered = difalInternoData.filter(item => item.eff_pct >= minVal);
        renderDifalInternoTable(filtered);
    }
}

// Redirect from KPI card click directly to that results section
function viewAuditSection(sectionId) {
    switchTab('auditResults');
    setTimeout(() => {
        const sec = document.getElementById(sectionId);
        if (sec) {
            sec.scrollIntoView({ behavior: 'smooth', block: 'start' });
        }
    }, 100);
}

// ----------------- EMAIL GENERATOR -----------------

function toggleSelectAllInTable(headerCb) {
    const table = headerCb.closest('table');
    const checkboxes = table.querySelectorAll('tbody input[type="checkbox"]');
    checkboxes.forEach(cb => {
        cb.checked = headerCb.checked;
    });
}

// Global helper to extract only first word for cleaner emitter names in emails
function getFirstWord(nameStr) {
    if (!nameStr) return 'Emitente';
    // If the name starts with "0 - " or "1 - ", strip that indicator first
    let clean = nameStr.replace(/^[01]\s*-\s*/, '').trim();
    return clean.split(' ')[0];
}

function generateDivergenciesEmail() {
    const checkedBoxes = document.querySelectorAll('.email-select-cb:checked');
    if (checkedBoxes.length === 0) {
        alert('Selecione ao menos uma divergência nas tabelas acima!');
        return;
    }
    
    const companyName = currentAuditData ? currentAuditData.company_name : "Cliente";
    const companyCnpj = currentAuditData ? currentAuditData.company_cnpj : "";
    
    // Set Subject
    document.getElementById('emailSubject').value = `Conferência Fiscal - Divergências Identificadas - ${companyName}`;
    
    let nfeLines = [];
    let cteLines = [];
    
    checkedBoxes.forEach(cb => {
        const item = emailItemsMap[cb.value];
        if (!item) return;
        
        const firstWordEmit = getFirstWord(item.emit);
        
        if (item.type === 'NFe') {
            // N° NF - RAZÃO SOCIAL EMITENTE - Item "descrição do produto" e as alterações que precisam ser feitas
            nfeLines.push(`Nº NF ${item.num} - ${firstWordEmit} - Item "${item.descr}" - ${item.details}`);
        } else if (item.type === 'CTe') {
            // Nº CTE - RAZAO SOCIAL EMITENTE - Alteração identificada
            cteLines.push(`Nº CTE ${item.num} - ${firstWordEmit} - ${item.details}`);
        }
    });
    
    let body = `Prezado Cliente,\n\n`;
    body += `Na auditoria fiscal dos lançamentos de entrada da empresa ${companyName} (CNPJ: ${companyCnpj}), identificamos as seguintes inconsistências que exigem atenção e retificação:\n\n`;
    
    if (nfeLines.length > 0) {
        body += `--- DIVERGÊNCIAS EM NOTAS FISCAIS (NF-e) ---\n`;
        body += nfeLines.join('\n') + `\n\n`;
    }
    
    if (cteLines.length > 0) {
        body += `--- DIVERGÊNCIAS EM CONHECIMENTOS DE TRANSPORTE (CT-e) ---\n`;
        body += cteLines.join('\n') + `\n\n`;
    }
    
    body += `Favor analisar e providenciar os devidos ajustes e retificações.\n\n`;
    body += `Atenciosamente,\n`;
    body += `Equipe de Auditoria Fiscal`;
    
    document.getElementById('emailBody').value = body;
    document.getElementById('emailDraftContainer').style.display = 'block';
    
    // Smooth scroll to email container
    document.getElementById('email-generator-sec').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

async function copyEmailToClipboard() {
    const emailBody = document.getElementById('emailBody');
    try {
        await navigator.clipboard.writeText(emailBody.value);
        alert('Rascunho do e-mail copiado para a área de transferência!');
    } catch (err) {
        emailBody.select();
        document.execCommand('copy');
        alert('Copiado com sucesso!');
    }
}

// ----------------- ICMS RULES CRUD -----------------

async function loadIcmsRules() {
    try {
        const response = await fetch('/api/rules/icms');
        const rules = await response.json();
        
        elements.tableIcmsRulesList.innerHTML = '';
        rules.forEach(rule => {
            const tr = document.createElement('tr');
            tr.innerHTML = `
                <td><strong>${rule.cfop}</strong></td>
                <td>
                    <span class="badge ${rule.allow_credit === 1 ? 'badge-danger' : 'badge-secondary'}">
                        ${rule.allow_credit === 1 ? 'Sim (Permite)' : 'Não (Material de consumo / Outros)'}
                    </span>
                </td>
                <td>${rule.description}</td>
                <td class="text-center">
                    <button class="btn btn-danger" style="padding: 4px 8px; font-size: 11px;" onclick="deleteIcmsRule('${rule.cfop}')">
                        <i class="fa-regular fa-trash-can"></i>
                    </button>
                </td>
            `;
            elements.tableIcmsRulesList.appendChild(tr);
        });
    } catch (error) {
        console.error('Error loading rules:', error);
    }
}

async function saveIcmsRule(event) {
    event.preventDefault();
    const cfop = document.getElementById('ruleCfop').value;
    const allow_credit = document.getElementById('ruleCredit').value;
    const description = document.getElementById('ruleDesc').value;
    
    try {
        const response = await fetch('/api/rules/icms', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ cfop, allow_credit, description })
        });
        if (response.ok) {
            alert('Regra tributária salva com sucesso!');
            document.getElementById('icmsRuleForm').reset();
            loadIcmsRules();
        } else {
            const err = await response.json();
            alert('Erro: ' + err.error);
        }
    } catch (error) {
        alert('Erro ao salvar regra.');
    }
}

async function deleteIcmsRule(cfop) {
    if (!confirm(`Deseja realmente excluir a regra para o CFOP ${cfop}?`)) return;
    try {
        await fetch(`/api/rules/icms/${cfop}`, { method: 'DELETE' });
        loadIcmsRules();
    } catch (error) {
        alert('Erro ao excluir regra.');
    }
}

// ----------------- CFOP PRODUCT CLASSIFICATION CRUD -----------------

async function loadClassifications() {
    try {
        const response = await fetch('/api/rules/classification');
        const list = await response.json();
        
        elements.tableClassList.innerHTML = '';
        list.forEach(item => {
            const tr = document.createElement('tr');
            tr.innerHTML = `
                <td data-t="s"><code>${item.supplier_cnpj}</code></td>
                <td><span class="badge badge-secondary">${item.supplier_cfop}</span></td>
                <td><code>${item.product_code}</code></td>
                <td>${item.product_description}</td>
                <td><span class="badge badge-blue"><strong>${item.entry_cfop}</strong></span></td>
                <td class="text-center">
                    <button class="btn btn-danger" style="padding: 4px 8px; font-size: 11px;" onclick="deleteClassRule('${item.company_cnpj}', '${item.supplier_cnpj}', '${item.supplier_cfop}', '${item.product_code}')">
                        <i class="fa-regular fa-trash-can"></i>
                    </button>
                </td>
            `;
            elements.tableClassList.appendChild(tr);
        });
    } catch (error) {
        console.error('Error loading classifications:', error);
    }
}

async function saveClassRule(event) {
    event.preventDefault();
    const company_cnpj = document.getElementById('classCnpj').value;
    const supplier_cnpj = document.getElementById('classSupplierCnpj').value;
    const supplier_cfop = document.getElementById('classSupplierCfop').value;
    const product_code = document.getElementById('classProductCode').value;
    const product_description = document.getElementById('classProductDesc').value;
    const entry_cfop = document.getElementById('classEntryCfop').value;
    
    try {
        const response = await fetch('/api/rules/classification', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ company_cnpj, supplier_cnpj, supplier_cfop, product_code, product_description, entry_cfop })
        });
        if (response.ok) {
            alert('Classificação salva com sucesso!');
            document.getElementById('cfopClassForm').reset();
            document.getElementById('classCnpj').value = company_cnpj; // Keep default CNPJ
            loadClassifications();
        } else {
            const err = await response.json();
            alert('Erro: ' + err.error);
        }
    } catch (error) {
        alert('Erro ao salvar classification.');
    }
}

async function deleteClassRule(cnpj, supplier_cnpj, supplier_cfop, product_code) {
    if (!confirm(`Deseja excluir a regra de entrada para o produto ${product_code} com CFOP ${supplier_cfop} do fornecedor ${supplier_cnpj}?`)) return;
    try {
        await fetch(`/api/rules/classification/${cnpj}/${supplier_cnpj}/${supplier_cfop}/${product_code}`, { method: 'DELETE' });
        loadClassifications();
    } catch (error) {
        alert('Erro ao excluir classificação.');
    }
}

// Save a new classification inline from unmapped products list
async function saveNewItemClassification(rowIndex, inputId) {
    if (!currentAuditData || !currentAuditData.unmapped_items || !currentAuditData.unmapped_items[rowIndex]) {
        alert("Erro: Dados do produto não encontrados no relatório atual.");
        return;
    }
    const item = currentAuditData.unmapped_items[rowIndex];
    const entryCfop = document.getElementById(inputId).value;
    const companyCnpj = currentAuditData.company_cnpj || "13639387000172";
    
    if (!entryCfop || entryCfop.length !== 4) {
        alert("Forneça um CFOP de entrada válido de 4 dígitos!");
        return;
    }
    
    try {
        const response = await fetch('/api/rules/classification', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                company_cnpj: companyCnpj,
                supplier_cnpj: item.supplier_cnpj,
                supplier_cfop: item.supplier_cfop,
                product_code: item.product_code,
                product_description: item.product_description,
                entry_cfop: entryCfop
            })
        });
        if (response.ok) {
            // Remove item from data array
            currentAuditData.unmapped_items.splice(rowIndex, 1);
            
            // Re-render table and update badges
            renderNewProductsTable(currentAuditData.unmapped_items);
            
            // Apply current filters so they persist on screen
            filterNewProducts();
            
            // Adjust KPI count
            elements.kpiUnmapped.textContent = currentAuditData.unmapped_items.length + currentAuditData.unmapped_cte_items.length;
            elements.badgeNewProducts.textContent = currentAuditData.unmapped_items.length;
            
            // Reload classifications database list
            loadClassifications();
        } else {
            const err = await response.json();
            alert('Erro: ' + err.error);
        }
    } catch (error) {
        alert('Erro ao salvar classificação.');
    }
}

// ----------------- CT-E CFOP CLASSIFICATION CRUD -----------------

async function loadCteClassifications() {
    try {
        const response = await fetch('/api/rules/cte-classification');
        const list = await response.json();
        
        elements.tableCteClassList.innerHTML = '';
        list.forEach(item => {
            const tr = document.createElement('tr');
            tr.innerHTML = `
                <td><span class="badge badge-secondary">${item.supplier_cfop}</span></td>
                <td><span class="badge badge-purple">${item.emitter_uf}</span></td>
                <td><span class="badge badge-blue"><strong>${item.entry_cfop}</strong></span></td>
                <td class="text-center">
                    <button class="btn btn-danger" style="padding: 4px 8px; font-size: 11px;" onclick="deleteCteClassRule('${item.company_cnpj}', '${item.supplier_cfop}', '${item.emitter_uf}')">
                        <i class="fa-regular fa-trash-can"></i>
                    </button>
                </td>
            `;
            elements.tableCteClassList.appendChild(tr);
        });
    } catch (error) {
        console.error('Error loading CT-e classifications:', error);
    }
}

async function saveCteClassRule(event) {
    event.preventDefault();
    const company_cnpj = document.getElementById('cteClassCnpj').value;
    const supplier_cfop = document.getElementById('cteSupplierCfop').value;
    const emitter_uf = document.getElementById('cteEmitterUf').value.toUpperCase();
    const entry_cfop = document.getElementById('cteEntryCfop').value;
    
    try {
        const response = await fetch('/api/rules/cte-classification', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ company_cnpj, supplier_cfop, emitter_uf, entry_cfop })
        });
        if (response.ok) {
            alert('Regra de CTe salva com sucesso!');
            document.getElementById('cteClassForm').reset();
            document.getElementById('cteClassCnpj').value = company_cnpj;
            loadCteClassifications();
        } else {
            const err = await response.json();
            alert('Erro: ' + err.error);
        }
    } catch (error) {
        alert('Erro ao salvar regra de CTe.');
    }
}

async function deleteCteClassRule(cnpj, supplier_cfop, emitter_uf) {
    if (!confirm(`Deseja excluir a regra de entrada para o CTe com CFOP ${supplier_cfop} e UF ${emitter_uf}?`)) return;
    try {
        await fetch(`/api/rules/cte-classification/${cnpj}/${supplier_cfop}/${emitter_uf}`, { method: 'DELETE' });
        loadCteClassifications();
    } catch (error) {
        alert('Erro ao excluir regra de CTe.');
    }
}

async function saveNewCteClassification(rowIndex, inputId) {
    if (!currentAuditData || !currentAuditData.unmapped_cte_items || !currentAuditData.unmapped_cte_items[rowIndex]) {
        alert("Erro: Dados do CTe não encontrados no relatório atual.");
        return;
    }
    const item = currentAuditData.unmapped_cte_items[rowIndex];
    const entryCfop = document.getElementById(inputId).value;
    const companyCnpj = currentAuditData.company_cnpj || "13639387000172";
    
    if (!entryCfop || entryCfop.length !== 4) {
        alert("Forneça um CFOP de entrada válido de 4 dígitos!");
        return;
    }
    
    try {
        const response = await fetch('/api/rules/cte-classification', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                company_cnpj: companyCnpj,
                supplier_cfop: item.supplier_cfop,
                emitter_uf: item.emitter_uf,
                entry_cfop: entryCfop
            })
        });
        if (response.ok) {
            const row = document.getElementById(`unmapped_cte_row_${rowIndex}`);
            if (row) {
                row.remove();
            }
            
            // Adjust unmapped counters
            const currentCount = parseInt(elements.badgeNewCteCfops.textContent) - 1;
            if (elements.badgeNewCteCfops) {
                elements.badgeNewCteCfops.textContent = currentCount;
            }
            elements.kpiUnmapped.textContent = parseInt(elements.kpiUnmapped.textContent) - 1;
            
            if (currentCount === 0) {
                elements.tableNewCteCfops.innerHTML = '<tr><td colspan="6" class="text-center text-success"><i class="fa-solid fa-circle-check"></i> Todos os CT-es já estão classificados!</td></tr>';
                const newCteSection = document.getElementById('newCteCfopsSection');
                if (newCteSection) newCteSection.style.display = 'none';
            }
            
            // Reload list
            loadCteClassifications();
            alert('Regra de CTe cadastrada com sucesso!');
        } else {
            const err = await response.json();
            alert('Erro: ' + err.error);
        }
    } catch (error) {
        alert('Erro ao salvar regra de CTe.');
    }
}

// ----------------- EXCEL EXPORT -----------------

function exportTableToExcel(tableId, filename) {
    const table = document.getElementById(tableId);
    if (!table) return;
    
    const clonedTable = table.cloneNode(true);
    clonedTable.querySelectorAll('input, button').forEach(el => el.remove());
    
    const wb = XLSX.utils.table_to_book(clonedTable, { sheet: "Relatório" });
    XLSX.writeFile(wb, filename);
}

// ----------------- UI UTILITIES -----------------

function showLoading(show, message = "Processando...") {
    if (show) {
        elements.loadingOverlay.querySelector('p').textContent = message;
        elements.loadingOverlay.classList.add('active');
    } else {
        elements.loadingOverlay.classList.remove('active');
    }
}
