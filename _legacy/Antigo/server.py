import os
import re
import sqlite3
import xml.etree.ElementTree as ET
from flask import Flask, request, jsonify, send_from_directory, Response

app = Flask(__name__)

# Default file paths
import json

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "audit_rules.db")
SPED_PATH = ""
ENTRADA_DIR = ""
CTE_DIR = ""

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

def load_config():
    global SPED_PATH, ENTRADA_DIR, CTE_DIR
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                SPED_PATH = data.get("sped_path", "")
                ENTRADA_DIR = data.get("entrada_dir", "")
                CTE_DIR = data.get("cte_dir", "")
        except Exception:
            pass

def save_config_to_file():
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "sped_path": SPED_PATH,
                "entrada_dir": ENTRADA_DIR,
                "cte_dir": CTE_DIR
            }, f, indent=4, ensure_ascii=False)
    except Exception:
        pass

# ----------------- EMBEDDED FRONTEND JAVASCRIPT -----------------
# We serve JS from memory to avoid OneDrive security policies deleting JS files on disk.
JS_CONTENT = r"""// Global UI Elements and State
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
"""

# ----------------- DATABASE HELPERS -----------------

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    cursor = conn.cursor()
    
    # 1. Table for ICMS Credit Rules by CFOP
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS cfop_credit_rules (
            cfop TEXT PRIMARY KEY,
            allow_credit INTEGER,
            description TEXT
        )
    """)
    
    # 2. Table for CFOP Classification mappings (Supplier CNPJ + Supplier CFOP + Product -> Entry CFOP)
    cursor.execute("PRAGMA table_info(cfop_classifications)")
    columns = [row[1] for row in cursor.fetchall()]
    if columns and "supplier_cnpj" not in columns:
        cursor.execute("DROP TABLE cfop_classifications")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS cfop_classifications (
            company_cnpj TEXT,
            supplier_cnpj TEXT,
            supplier_cfop TEXT,
            product_code TEXT,
            product_description TEXT,
            entry_cfop TEXT,
            PRIMARY KEY (company_cnpj, supplier_cnpj, supplier_cfop, product_code)
        )
    """)
    
    # 3. Table for CT-e CFOP Classification mappings (Supplier CFOP + Emitter UF -> Entry CFOP)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS cte_cfop_classifications (
            company_cnpj TEXT,
            supplier_cfop TEXT,
            emitter_uf TEXT,
            entry_cfop TEXT,
            PRIMARY KEY (company_cnpj, supplier_cfop, emitter_uf)
        )
    """)
    
    # Pre-populate default ICMS credit rules
    default_rules = [
        # CFOPs that DO NOT allow credit
        ("1556", 0, "Compra de material para uso ou consumo"),
        ("2556", 0, "Compra de material para uso ou consumo (Interestadual)"),
        ("1407", 0, "Compra de mercadoria para uso ou consumo com substituição tributária"),
        ("2407", 0, "Compra de mercadoria para uso ou consumo com ST (Interestadual)"),
        ("1653", 0, "Compra de combustível ou lubrificante por consumidor ou usuário final"),
        ("2653", 0, "Compra de combustível ou lubrificante por consumidor/usuário final (Interestadual)"),
        ("1403", 0, "Compra para comercialização em operação com mercadoria sujeita ao regime de ST"),
        ("2403", 0, "Compra para comercialização em operação com mercadoria sujeita ao regime de ST (Interestadual)"),
        ("1303", 0, "Aquisição de serviço de comunicação por consumidor ou usuário final"),
        ("2303", 0, "Aquisição de serviço de comunicação por consumidor/usuário final (Interestadual)"),
        ("1551", 0, "Compra de bem para o ativo imobilizado"),
        ("2551", 0, "Compra de bem para o ativo imobilizado (Interestadual)"),
        ("1406", 0, "Compra de bem para o ativo imobilizado com substituição tributária"),
        ("2406", 0, "Compra de bem para o ativo imobilizado com ST (Interestadual)"),
        ("1652", 0, "Compra de combustível ou lubrificante para consumo em processo de industrialização/ativo imobilizado"),
        ("2652", 0, "Compra de combustível ou lubrificante para consumo in industrialização/ativo imobilizado (Interestadual)"),
        ("1661", 0, "Compra de serviço de transporte de bem para o ativo imobilizado"),
        ("2661", 0, "Compra de serviço de transporte de bem para o ativo imobilizado (Interestadual)"),
        ("1662", 0, "Compra de serviço de transporte de material para uso ou consumo"),
        ("2662", 0, "Compra de serviço de transporte de material para uso ou consumo (Interestadual)"),
        
        # CFOPs that DO allow credit
        ("1101", 1, "Compra para industrialização ou produção rural"),
        ("2101", 1, "Compra para industrialização ou produção rural (Interestadual)"),
        ("1102", 1, "Compra para comercialização"),
        ("2102", 1, "Compra para comercialização (Interestadual)"),
        ("1910", 1, "Entrada de bonificação, doação ou brinde (Permite crédito se destinado à comercialização/revenda)"),
        ("2910", 1, "Entrada de bonificação, doação ou brinde (Interestadual, permite crédito se para revenda)"),
    ]
    
    for cfop, allow, desc in default_rules:
        cursor.execute("""
            INSERT OR IGNORE INTO cfop_credit_rules (cfop, allow_credit, description)
            VALUES (?, ?, ?)
        """, (cfop, allow, desc))
        
    conn.commit()
    conn.close()

# ----------------- PARSING UTILITIES -----------------

def clean_float(val):
    if not val:
        return 0.0
    val = val.strip().replace(".", "").replace(",", ".")
    try:
        return float(val)
    except ValueError:
        return 0.0

def parse_sped(path):
    if not os.path.exists(path):
        raise FileNotFoundError(f"Arquivo SPED não encontrado: {path}")
        
    company_cnpj = ""
    company_name = ""
    c100_entries = {}
    d100_entries = {}
    prod_descriptions = {}
    participants = {}
    
    with open(path, "r", encoding="latin1") as f:
        current_c100 = None
        current_d100 = None
        for line in f:
            if not line.startswith("|"):
                continue
            fields = line.strip().split("|")
            if len(fields) < 2:
                continue
            reg = fields[1]
            
            if reg == "0000":
                company_cnpj = fields[7] if len(fields) > 7 else ""
                company_name = fields[6] if len(fields) > 6 else ""
            elif reg == "0150":
                cod_part = fields[2] if len(fields) > 2 else ""
                nome_part = fields[3] if len(fields) > 3 else ""
                participants[cod_part] = nome_part
            elif reg == "0200":
                cod_item = fields[2] if len(fields) > 2 else ""
                descr = fields[3] if len(fields) > 3 else ""
                prod_descriptions[cod_item] = descr
            elif reg == "C100":
                oper = fields[2] if len(fields) > 2 else ""
                ind_emit = fields[3] if len(fields) > 3 else ""
                emit = fields[4] if len(fields) > 4 else ""
                mod = fields[5] if len(fields) > 5 else ""
                sit = fields[6] if len(fields) > 6 else ""
                num = fields[8] if len(fields) > 8 else ""
                key = fields[9] if len(fields) > 9 else ""
                vl_doc = clean_float(fields[12]) if len(fields) > 12 else 0.0
                vl_icms = clean_float(fields[22]) if len(fields) > 22 else 0.0
                vl_icms_st = clean_float(fields[24]) if len(fields) > 24 else 0.0
                
                # Only care about regular inbound notes (oper=0, sit=00)
                if oper == "0" and sit == "00":
                    current_c100 = key
                    c100_entries[key] = {
                        "num": num,
                        "key": key,
                        "mod": mod,
                        "ind_emit": ind_emit,
                        "emit": emit,
                        "vl_doc": vl_doc,
                        "vl_icms": vl_icms,
                        "vl_icms_st": vl_icms_st,
                        "items": []
                    }
                else:
                    current_c100 = None
            elif reg == "C170" and current_c100:
                # Item of entry
                num_item = fields[2] if len(fields) > 2 else ""
                cod_item = fields[3] if len(fields) > 3 else ""
                qtd = clean_float(fields[5]) if len(fields) > 5 else 0.0
                vl_item = clean_float(fields[7]) if len(fields) > 7 else 0.0
                cst = fields[10] if len(fields) > 10 else ""
                cfop = fields[11] if len(fields) > 11 else ""
                vl_bc_icms = clean_float(fields[13]) if len(fields) > 13 else 0.0
                vl_icms = clean_float(fields[15]) if len(fields) > 15 else 0.0
                vl_bc_icms_st = clean_float(fields[16]) if len(fields) > 16 else 0.0
                vl_icms_st = clean_float(fields[18]) if len(fields) > 18 else 0.0
                
                c100_entries[current_c100]["items"].append({
                    "num_item": num_item,
                    "cod_item": cod_item,
                    "qtd": qtd,
                    "vl_item": vl_item,
                    "cst": cst,
                    "cfop": cfop,
                    "vl_bc_icms": vl_bc_icms,
                    "vl_icms": vl_icms,
                    "vl_bc_icms_st": vl_bc_icms_st,
                    "vl_icms_st": vl_icms_st
                })
            elif reg == "D100":
                oper = fields[2] if len(fields) > 2 else ""
                ind_emit = fields[3] if len(fields) > 3 else ""
                emit = fields[4] if len(fields) > 4 else ""
                mod = fields[5] if len(fields) > 5 else ""
                sit = fields[6] if len(fields) > 6 else ""
                num = fields[9] if len(fields) > 9 else ""
                key = fields[10] if len(fields) > 10 else ""
                vl_doc = clean_float(fields[15]) if len(fields) > 15 else 0.0
                vl_icms = clean_float(fields[22]) if len(fields) > 22 else 0.0
                
                if oper == "0" and sit == "00":
                    current_d100 = key
                    d100_entries[key] = {
                        "num": num,
                        "key": key,
                        "mod": mod,
                        "ind_emit": ind_emit,
                        "emit": emit,
                        "vl_doc": vl_doc,
                        "vl_icms": vl_icms,
                        "cfops": []
                    }
                else:
                    current_d100 = None
            elif reg == "D190" and current_d100:
                cfop = fields[3] if len(fields) > 3 else ""
                if cfop and cfop not in d100_entries[current_d100]["cfops"]:
                    d100_entries[current_d100]["cfops"].append(cfop)
                    
    return {
        "cnpj": company_cnpj,
        "name": company_name,
        "c100": c100_entries,
        "d100": d100_entries,
        "products": prod_descriptions,
        "participants": participants
    }

def parse_nfe_xml(path):
    try:
        tree = ET.parse(path)
        root = tree.getroot()
        ns = {"ns": "http://www.portalfiscal.inf.br/nfe"}
        
        # Emitter
        emit = root.find(".//ns:emit", ns)
        emit_cnpj = emit.find("ns:CNPJ", ns).text if emit is not None and emit.find("ns:CNPJ", ns) is not None else ""
        emit_cpf = emit.find("ns:CPF", ns).text if emit is not None and emit.find("ns:CPF", ns) is not None else ""
        emit_name = emit.find("ns:xNome", ns).text if emit is not None else ""
        emit_id = emit_cnpj if emit_cnpj else emit_cpf
        ender_emit = emit.find("ns:enderEmit", ns) if emit is not None else None
        emit_uf = ender_emit.find("ns:UF", ns).text if ender_emit is not None and ender_emit.find("ns:UF", ns) is not None else ""
        
        # Ide
        ide = root.find(".//ns:ide", ns)
        num = ide.find("ns:nNF", ns).text if ide is not None else ""
        serie = ide.find("ns:serie", ns).text if ide is not None else ""
        
        # Total value
        total = root.find(".//ns:total/ns:ICMSTot", ns)
        v_nf = float(total.find("ns:vNF", ns).text) if total is not None and total.find("ns:vNF", ns) is not None else 0.0
        v_st_total = float(total.find("ns:vST", ns).text) if total is not None and total.find("ns:vST", ns) is not None else 0.0
        
        # Items
        items = []
        for det in root.findall(".//ns:det", ns):
            n_item = det.attrib.get("nItem")
            prod = det.find("ns:prod", ns)
            c_prod = prod.find("ns:cProd", ns).text if prod is not None else ""
            x_prod = prod.find("ns:xProd", ns).text if prod is not None else ""
            
            # Product details
            v_prod_el = prod.find("ns:vProd", ns) if prod is not None else None
            v_prod = float(v_prod_el.text) if v_prod_el is not None and v_prod_el.text else 0.0
            
            v_frete_el = prod.find("ns:vFrete", ns) if prod is not None else None
            v_frete = float(v_frete_el.text) if v_frete_el is not None and v_frete_el.text else 0.0
            
            v_seg_el = prod.find("ns:vSeg", ns) if prod is not None else None
            v_seg = float(v_seg_el.text) if v_seg_el is not None and v_seg_el.text else 0.0
            
            v_outro_el = prod.find("ns:vOutro", ns) if prod is not None else None
            v_outro = float(v_outro_el.text) if v_outro_el is not None and v_outro_el.text else 0.0
            
            v_desc_el = prod.find("ns:vDesc", ns) if prod is not None else None
            v_desc = float(v_desc_el.text) if v_desc_el is not None and v_desc_el.text else 0.0
            
            cfop = prod.find("ns:CFOP", ns).text if prod is not None else ""
            ncm = prod.find("ns:NCM", ns).text if prod is not None and prod.find("ns:NCM", ns) is not None else ""
            cest = prod.find("ns:CEST", ns).text if prod is not None and prod.find("ns:CEST", ns) is not None else ""
            c_benef = prod.find("ns:cBenef", ns).text if prod is not None and prod.find("ns:cBenef", ns) is not None else ""
            
            # ST/FCP/IPI values inside det
            v_fcp_st_el = det.find(".//ns:vFCPST", ns)
            fcp_st_val = float(v_fcp_st_el.text) if v_fcp_st_el is not None and v_fcp_st_el.text else 0.0
            
            v_ipi_el = det.find(".//ns:vIPI", ns)
            v_ipi = float(v_ipi_el.text) if v_ipi_el is not None and v_ipi_el.text else 0.0
            
            v_ipi_devol_el = det.find(".//ns:vIPIDevol", ns)
            v_ipi_devol = float(v_ipi_devol_el.text) if v_ipi_devol_el is not None and v_ipi_devol_el.text else 0.0
            
            # ICMS parsing
            imposto = det.find("ns:imposto", ns)
            icms_val = 0.0
            icms_st_val = 0.0
            v_bc = 0.0
            p_icms = 0.0
            cst_icms = ""
            
            if imposto is not None:
                icms = imposto.find("ns:ICMS", ns)
                if icms is not None:
                    for child in icms:
                        cst_el = child.find("ns:CST", ns)
                        csosn_el = child.find("ns:CSOSN", ns)
                        cst_icms = cst_el.text if cst_el is not None else (csosn_el.text if csosn_el is not None else "")
                        
                        v_bc_el = child.find("ns:vBC", ns)
                        if v_bc_el is not None and v_bc_el.text:
                            v_bc = float(v_bc_el.text)
                            
                        p_icms_el = child.find("ns:pICMS", ns)
                        if p_icms_el is not None and p_icms_el.text:
                            p_icms = float(p_icms_el.text)
                            
                        v_icms_el = child.find("ns:vICMS", ns)
                        if v_icms_el is not None and v_icms_el.text:
                            icms_val += float(v_icms_el.text)
                        
                        v_cred_el = child.find("ns:vCredICMSSN", ns)
                        if v_cred_el is not None and v_cred_el.text:
                            icms_val += float(v_cred_el.text)
                            
                        v_icms_st_el = child.find("ns:vICMSST", ns)
                        if v_icms_st_el is not None and v_icms_st_el.text:
                            icms_st_val = float(v_icms_st_el.text)
                            
            # Calculate full item cost: Produto + Frete + Seguro + Outras Despesas + ICMS ST + FCP ST + IPI + IPI Devolvido - Desconto
            vl_item_calculado = v_prod + v_frete + v_seg + v_outro + icms_st_val + fcp_st_val + v_ipi + v_ipi_devol - v_desc
            
            items.append({
                "num_item": n_item,
                "cod_item": c_prod,
                "descr": x_prod,
                "cfop": cfop,
                "cst": cst_icms,
                "ncm": ncm,
                "cest": cest,
                "c_benef": c_benef,
                "vl_item": vl_item_calculado,
                "vl_prod_net": v_prod - v_desc,
                "vl_bc": v_bc,
                "p_icms": p_icms,
                "vl_icms": icms_val,
                "vl_icms_st": icms_st_val,
                "fcp_st": fcp_st_val,
                "v_ipi": v_ipi,
                "v_ipi_devol": v_ipi_devol
            })
            
        return {
            "num": num,
            "serie": serie,
            "emit_id": emit_id,
            "emit_name": emit_name,
            "emit_uf": emit_uf,
            "vl_doc": v_nf,
            "vl_st": v_st_total,
            "items": items
        }
    except Exception as e:
        return {"error": str(e)}

def parse_cte_xml(path):
    try:
        tree = ET.parse(path)
        root = tree.getroot()
        ns = {"ns": "http://www.portalfiscal.inf.br/cte"}
        
        # Ide
        ide = root.find(".//ns:ide", ns)
        num = ide.find("ns:nCT", ns).text if ide is not None else ""
        serie = ide.find("ns:serie", ns).text if ide is not None else ""
        cfop_el = ide.find("ns:CFOP", ns) if ide is not None else None
        cfop = cfop_el.text if cfop_el is not None else ""
        
        # Emitter
        emit = root.find(".//ns:emit", ns)
        emit_cnpj = emit.find("ns:CNPJ", ns).text if emit is not None and emit.find("ns:CNPJ", ns) is not None else ""
        emit_name = emit.find("ns:xNome", ns).text if emit is not None else ""
        ender_emit = emit.find("ns:enderEmit", ns) if emit is not None else None
        emit_uf = ender_emit.find("ns:UF", ns).text if ender_emit is not None and ender_emit.find("ns:UF", ns) is not None else ""
        
        # Value of service
        v_prest = root.find(".//ns:vPrest", ns)
        v_t_prest = 0.0
        if v_prest is not None:
            v_t_prest_el = v_prest.find("ns:vTPrest", ns)
            if v_t_prest_el is None:
                v_t_prest_el = v_prest.find("ns:vTprest", ns)
            v_t_prest = float(v_t_prest_el.text) if v_t_prest_el is not None and v_t_prest_el.text else 0.0
        
        # ICMS and ICMS BC
        imposto = root.find(".//ns:imp", ns)
        icms_val = 0.0
        v_bc = 0.0
        if imposto is not None:
            icms = imposto.find("ns:ICMS", ns)
            if icms is not None:
                for child in icms:
                    v_icms_el = child.find("ns:vICMS", ns)
                    if v_icms_el is not None and v_icms_el.text:
                        icms_val = float(v_icms_el.text)
                    v_bc_el = child.find("ns:vBC", ns)
                    if v_bc_el is not None and v_bc_el.text:
                        v_bc = float(v_bc_el.text)
                        
        # Participant names
        rem_name_el = root.find(".//ns:rem/ns:xNome", ns)
        rem_name = rem_name_el.text if rem_name_el is not None else ""
        
        dest_name_el = root.find(".//ns:dest/ns:xNome", ns)
        dest_name = dest_name_el.text if dest_name_el is not None else ""
        
        exped_name_el = root.find(".//ns:exped/ns:xNome", ns)
        exped_name = exped_name_el.text if exped_name_el is not None else ""
        
        receb_name_el = root.find(".//ns:receb/ns:xNome", ns)
        receb_name = receb_name_el.text if receb_name_el is not None else ""
        
        # Tomador (Taker)
        toma_cnpj = ""
        toma_cpf = ""
        toma_role = ""
        toma_name = ""
        
        toma3 = ide.find("ns:toma3", ns)
        toma4 = ide.find("ns:toma4", ns)
        
        if toma4 is not None:
            toma_role = "outros"
            t_name_el = toma4.find("ns:xNome", ns)
            toma_name = t_name_el.text if t_name_el is not None else ""
            cnpj_el = toma4.find("ns:CNPJ", ns)
            toma_cnpj = cnpj_el.text if cnpj_el is not None else ""
            cpf_el = toma4.find("ns:CPF", ns)
            toma_cpf = cpf_el.text if cpf_el is not None else ""
        elif toma3 is not None:
            toma_val = toma3.find("ns:toma", ns).text if toma3.find("ns:toma", ns) is not None else ""
            role_map = {"0": "rem", "1": "exped", "2": "receb", "3": "dest"}
            toma_role = role_map.get(toma_val, "")
            
            if toma_role == "rem":
                toma_name = rem_name
            elif toma_role == "dest":
                toma_name = dest_name
            elif toma_role == "exped":
                toma_name = exped_name
            elif toma_role == "receb":
                toma_name = receb_name
                
            if toma_role:
                role_el = root.find(f".//ns:{toma_role}", ns)
                if role_el is not None:
                    cnpj_el = role_el.find("ns:CNPJ", ns)
                    toma_cnpj = cnpj_el.text if cnpj_el is not None else ""
                    cpf_el = role_el.find("ns:CPF", ns)
                    toma_cpf = cpf_el.text if cpf_el is not None else ""
        
        toma_id = toma_cnpj if toma_cnpj else toma_cpf
        
        # Invoices carried
        nfe_keys = []
        nfe_nums = []
        inf_nfes = root.findall(".//ns:infNFe", ns)
        for inf_nfe in inf_nfes:
            chave_el = inf_nfe.find("ns:chave", ns)
            if chave_el is not None and chave_el.text:
                k = chave_el.text.strip()
                nfe_keys.append(k)
                if len(k) == 44:
                    nfe_nums.append(str(int(k[25:34])))
                else:
                    nfe_nums.append("N/A")
        
        return {
            "num": num,
            "serie": serie,
            "emit_id": emit_cnpj,
            "emit_name": emit_name,
            "emit_uf": emit_uf,
            "cfop": cfop,
            "vl_doc": v_t_prest,
            "vl_bc": v_bc,
            "vl_icms": icms_val,
            "tomador_id": toma_id,
            "tomador_name": toma_name,
            "tomador_role": toma_role,
            "rem_name": rem_name,
            "dest_name": dest_name,
            "nfe_keys": nfe_keys,
            "nfe_nums": nfe_nums
        }
    except Exception as e:
        return {"error": str(e)}

# ----------------- API ENDPOINTS -----------------

@app.route("/")
def index():
    return send_from_directory("static", "index.html")

@app.route("/app.js")
def serve_app_js():
    return Response(JS_CONTENT, mimetype="application/javascript")

@app.route("/api/browse", methods=["GET"])
def browse_path():
    browse_type = request.args.get("type", "dir")
    current_path = request.args.get("current", "")
    
    # Run Tkinter in a safe desktop context
    try:
        import tkinter as tk
        from tkinter import filedialog
        
        initial_dir = ""
        if current_path:
            current_path = current_path.strip().replace('"', '')
            if os.path.isfile(current_path):
                initial_dir = os.path.dirname(current_path)
            elif os.path.isdir(current_path):
                initial_dir = current_path
        
        root = tk.Tk()
        root.withdraw() # Hide root Tk window
        root.attributes("-topmost", True) # Force dialog to front
        
        selected_path = ""
        if browse_type == "file":
            selected_path = filedialog.askopenfilename(
                initialdir=initial_dir if initial_dir and os.path.exists(initial_dir) else None,
                title="Selecione o arquivo SPED Fiscal (.txt)",
                filetypes=[("Arquivos de Texto (*.txt)", "*.txt"), ("Todos os Arquivos", "*.*")]
            )
        else:
            selected_path = filedialog.askdirectory(
                initialdir=initial_dir if initial_dir and os.path.exists(initial_dir) else None,
                title="Selecione a Pasta de XMLs"
            )
            
        root.destroy()
        
        if selected_path:
            selected_path = os.path.abspath(selected_path)
            
        return jsonify({"status": "success", "path": selected_path})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/<path:path>")
def serve_static(path):
    return send_from_directory("static", path)

@app.route("/api/config", methods=["GET", "POST"])
def manage_config():
    global SPED_PATH, ENTRADA_DIR, CTE_DIR, DB_PATH
    if request.method == "POST":
        try:
            req = request.json or {}
            SPED_PATH = req.get("sped_path", SPED_PATH)
            ENTRADA_DIR = req.get("entrada_dir", ENTRADA_DIR)
            CTE_DIR = req.get("cte_dir", CTE_DIR)
            save_config_to_file()
            
            new_db = req.get("db_path")
            if new_db and new_db != DB_PATH:
                DB_PATH = new_db
                init_db()
            elif not os.path.exists(DB_PATH):
                init_db()
        except Exception as e:
            import traceback
            print("ERROR saving paths:")
            traceback.print_exc()
            return jsonify({"status": "error", "message": str(e)}), 500
            
    # Validate paths
    config_status = {
        "sped_path": SPED_PATH,
        "sped_exists": os.path.exists(SPED_PATH),
        "entrada_dir": ENTRADA_DIR,
        "entrada_exists": os.path.isdir(ENTRADA_DIR),
        "cte_dir": CTE_DIR,
        "cte_exists": os.path.isdir(CTE_DIR),
        "db_path": DB_PATH,
        "db_exists": os.path.exists(DB_PATH)
    }
    return jsonify(config_status)

@app.route("/api/rules/icms", methods=["GET", "POST"])
def icms_rules():
    conn = get_db()
    cursor = conn.cursor()
    
    if request.method == "POST":
        req = request.json
        cfop = req.get("cfop")
        allow_credit = int(req.get("allow_credit", 0))
        description = req.get("description", "")
        
        if not cfop or len(cfop) != 4:
            return jsonify({"error": "CFOP inválido"}), 400
            
        cursor.execute("""
            INSERT INTO cfop_credit_rules (cfop, allow_credit, description)
            VALUES (?, ?, ?)
            ON CONFLICT(cfop) DO UPDATE SET
                allow_credit=excluded.allow_credit,
                description=excluded.description
        """, (cfop, allow_credit, description))
        conn.commit()
        
    cursor.execute("SELECT * FROM cfop_credit_rules ORDER BY cfop")
    rules = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return jsonify(rules)

@app.route("/api/rules/icms/<cfop>", methods=["DELETE"])
def delete_icms_rule(cfop):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM cfop_credit_rules WHERE cfop = ?", (cfop,))
    conn.commit()
    conn.close()
    return jsonify({"success": True})

@app.route("/api/rules/classification", methods=["GET", "POST"])
def classification_rules():
    conn = get_db()
    cursor = conn.cursor()
    
    if request.method == "POST":
        req = request.json
        company_cnpj = req.get("company_cnpj")
        supplier_cnpj = req.get("supplier_cnpj")
        supplier_cfop = req.get("supplier_cfop")
        product_code = req.get("product_code")
        product_description = req.get("product_description", "")
        entry_cfop = req.get("entry_cfop")
        
        if not company_cnpj or not supplier_cnpj or not supplier_cfop or not product_code or not entry_cfop:
            return jsonify({"error": "Dados obrigatórios ausentes"}), 400
            
        cursor.execute("""
            INSERT INTO cfop_classifications (company_cnpj, supplier_cnpj, supplier_cfop, product_code, product_description, entry_cfop)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(company_cnpj, supplier_cnpj, supplier_cfop, product_code) DO UPDATE SET
                entry_cfop=excluded.entry_cfop,
                product_description=excluded.product_description
        """, (company_cnpj, supplier_cnpj, supplier_cfop, product_code, product_description, entry_cfop))
        conn.commit()
        
    cursor.execute("SELECT * FROM cfop_classifications ORDER BY supplier_cfop, product_code")
    classifications = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return jsonify(classifications)

@app.route("/api/rules/classification/bulk", methods=["POST"])
def bulk_classification_rules():
    req = request.json or {}
    company_cnpj = req.get("company_cnpj")
    entry_cfop = req.get("entry_cfop")
    items = req.get("items", [])
    
    root_supplier_cnpj = req.get("supplier_cnpj")
    root_supplier_cfop = req.get("supplier_cfop")
    
    if not company_cnpj or not entry_cfop or not items:
        return jsonify({"error": "Dados obrigatórios ausentes: empresa, CFOP de entrada ou itens"}), 400
        
    conn = get_db()
    cursor = conn.cursor()
    try:
        inserted = 0
        for item in items:
            s_cnpj = item.get("supplier_cnpj") or root_supplier_cnpj
            s_cfop = item.get("supplier_cfop") or root_supplier_cfop
            p_code = item.get("product_code")
            p_desc = item.get("product_description", "")
            
            if not s_cnpj or not s_cfop or not p_code:
                continue
                
            cursor.execute("""
                INSERT INTO cfop_classifications (company_cnpj, supplier_cnpj, supplier_cfop, product_code, product_description, entry_cfop)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(company_cnpj, supplier_cnpj, supplier_cfop, product_code) DO UPDATE SET
                    entry_cfop=excluded.entry_cfop,
                    product_description=excluded.product_description
            """, (company_cnpj, s_cnpj, s_cfop, p_code, p_desc, entry_cfop))
            inserted += 1
            
        if inserted == 0:
            return jsonify({"error": "Nenhum item válido para classificação foi processado"}), 400
            
        conn.commit()
        return jsonify({"success": True, "count": inserted})
    except Exception as e:
        conn.rollback()
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

@app.route("/api/rules/classification/<company_cnpj>/<supplier_cnpj>/<supplier_cfop>/<product_code>", methods=["DELETE"])
def delete_classification(company_cnpj, supplier_cnpj, supplier_cfop, product_code):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        DELETE FROM cfop_classifications 
        WHERE company_cnpj = ? AND supplier_cnpj = ? AND supplier_cfop = ? AND product_code = ?
    """, (company_cnpj, supplier_cnpj, supplier_cfop, product_code))
    conn.commit()
    conn.close()
    return jsonify({"success": True})

@app.route("/api/rules/cte-classification", methods=["GET", "POST"])
def cte_classification_rules():
    conn = get_db()
    cursor = conn.cursor()
    
    if request.method == "POST":
        req = request.json
        company_cnpj = req.get("company_cnpj")
        supplier_cfop = req.get("supplier_cfop")
        emitter_uf = req.get("emitter_uf")
        entry_cfop = req.get("entry_cfop")
        
        if not company_cnpj or not supplier_cfop or not emitter_uf or not entry_cfop:
            return jsonify({"error": "Dados obrigatórios ausentes"}), 400
            
        cursor.execute("""
            INSERT INTO cte_cfop_classifications (company_cnpj, supplier_cfop, emitter_uf, entry_cfop)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(company_cnpj, supplier_cfop, emitter_uf) DO UPDATE SET
                entry_cfop=excluded.entry_cfop
        """, (company_cnpj, supplier_cfop, emitter_uf, entry_cfop))
        conn.commit()
        
    cursor.execute("SELECT * FROM cte_cfop_classifications ORDER BY supplier_cfop, emitter_uf")
    classifications = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return jsonify(classifications)

@app.route("/api/rules/cte-classification/<company_cnpj>/<supplier_cfop>/<emitter_uf>", methods=["DELETE"])
def delete_cte_classification(company_cnpj, supplier_cfop, emitter_uf):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        DELETE FROM cte_cfop_classifications 
        WHERE company_cnpj = ? AND supplier_cfop = ? AND emitter_uf = ?
    """, (company_cnpj, supplier_cfop, emitter_uf))
    conn.commit()
    conn.close()
    return jsonify({"success": True})

@app.route("/api/audit", methods=["GET"])
def run_audit():
    try:
        # Load SPED data
        sped_data = parse_sped(SPED_PATH)
        company_cnpj = sped_data["cnpj"]
        participants = sped_data["participants"]
        
        # Load ICMS Credit Rules from DB
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT cfop, allow_credit FROM cfop_credit_rules")
        icms_rules_map = {row["cfop"]: row["allow_credit"] for row in cursor.fetchall()}
        
        # Load CFOP Classification rules from DB
        cursor.execute("SELECT supplier_cnpj, supplier_cfop, product_code, entry_cfop FROM cfop_classifications WHERE company_cnpj = ?", (company_cnpj,))
        cfop_mappings = {}
        for row in cursor.fetchall():
            key = (row["supplier_cnpj"], row["supplier_cfop"], row["product_code"])
            cfop_mappings[key] = row["entry_cfop"]
            
        # Load CT-e CFOP Classification rules from DB
        cursor.execute("SELECT supplier_cfop, emitter_uf, entry_cfop FROM cte_cfop_classifications WHERE company_cnpj = ?", (company_cnpj,))
        cte_cfop_mappings = {}
        for row in cursor.fetchall():
            key = (row["supplier_cfop"], row["emitter_uf"])
            cte_cfop_mappings[key] = row["entry_cfop"]
            
        conn.close()
        
        # Scan folders for XML keys
        entrada_xmls = {}
        if os.path.isdir(ENTRADA_DIR):
            for f in os.listdir(ENTRADA_DIR):
                if f.endswith(".xml"):
                    key = f[:-4]
                    entrada_xmls[key] = os.path.join(ENTRADA_DIR, f)
                    
        cte_xmls = {}
        if os.path.isdir(CTE_DIR):
            for f in os.listdir(CTE_DIR):
                if f.endswith(".xml"):
                    key = f[:-4]
                    cte_xmls[key] = os.path.join(CTE_DIR, f)
        
        # Initialize Audit Lists
        missing_xmls = []
        value_discrepancies = []
        icms_issues = []
        icms_st_notes = []
        cte_tomador_issues = []
        cfop_classifications_issues = []
        cte_cfop_classifications_issues = []
        unmapped_items = []
        unmapped_cte_items = []
        difal_interno = []
        difal_interestadual = []
        cte_xml_list = []
        nfe_xml_list = []
        seen_unmapped = set()
        seen_unmapped_cte = set()
        used_cfops = set()
        
        total_sped_nfe = len(sped_data["c100"])
        total_sped_cte = len(sped_data["d100"])
        
        # 1. AUDIT C100 entries (NF-e)
        for key, c100 in sped_data["c100"].items():
            if c100["mod"] != "55":
                continue
                
            xml_path = entrada_xmls.get(key)
            if not xml_path:
                missing_xmls.append({
                    "type": "NF-e",
                    "key": key,
                    "num": c100["num"],
                    "mod": c100["mod"],
                    "ind_emit": c100.get("ind_emit", ""),
                    "emit": participants.get(c100["emit"], c100["emit"]),
                    "vl_doc": c100["vl_doc"]
                })
                continue
                
            xml_data = parse_nfe_xml(xml_path)
            if "error" in xml_data:
                continue
                
            diff_doc = abs(c100["vl_doc"] - xml_data["vl_doc"])
            if diff_doc > 0.02:
                value_discrepancies.append({
                    "type": "NF-e",
                    "key": key,
                    "num": c100["num"],
                    "emit_name": xml_data.get("emit_name", participants.get(c100["emit"], c100["emit"])),
                    "sped_val": c100["vl_doc"],
                    "xml_val": xml_data["vl_doc"],
                    "diff": diff_doc
                })
                
            if xml_data["vl_st"] > 0 or any(item["vl_icms_st"] > 0 for item in xml_data["items"]):
                icms_st_notes.append({
                    "key": key,
                    "num": c100["num"],
                    "emit_name": xml_data["emit_name"],
                    "vst_total": xml_data["vl_st"],
                    "items": [
                        {"num_item": item["num_item"], "descr": item["descr"], "vl_icms_st": item["vl_icms_st"]}
                        for item in xml_data["items"] if item["vl_icms_st"] > 0
                    ]
                })
                
            # Option C: Hybrid Matching with Fallback
            xml_items_list = xml_data["items"]
            already_matched = set()
            
            for item in c100["items"]:
                n_item = item["num_item"]
                sped_cfop = item["cfop"]
                sped_icms = item["vl_icms"]
                sped_bc = item["vl_bc_icms"]
                sped_val = item["vl_item"]
                sped_code = item["cod_item"]
                
                # Find matching XML item using Option C cascade
                xml_item = None
                
                # 1. Match by Product Code + Value check (tolerance of R$ 0.05)
                for i, x_item in enumerate(xml_items_list):
                    if i in already_matched:
                        continue
                    if sped_code == x_item["cod_item"]:
                        if abs(sped_val - x_item.get("vl_prod_net", x_item["vl_item"])) < 0.05:
                            xml_item = x_item
                            already_matched.add(i)
                            break
                            
                # 2. Match by Item Number + Value check (tolerance of R$ 0.05)
                if not xml_item:
                    for i, x_item in enumerate(xml_items_list):
                        if i in already_matched:
                            continue
                        try:
                            match_num = int(n_item) == int(x_item["num_item"])
                        except ValueError:
                            match_num = str(n_item) == str(x_item["num_item"])
                        if match_num:
                            if abs(sped_val - x_item.get("vl_prod_net", x_item["vl_item"])) < 0.05:
                                xml_item = x_item
                                already_matched.add(i)
                                break
                                
                # 3. Match by combination of values (Value + ICMS + Base)
                if not xml_item:
                    for i, x_item in enumerate(xml_items_list):
                        if i in already_matched:
                            continue
                        val_diff = abs(sped_val - x_item.get("vl_prod_net", x_item["vl_item"]))
                        icms_diff = abs(sped_icms - x_item["vl_icms"])
                        bc_diff = abs(sped_bc - x_item["vl_bc"])
                        if val_diff < 0.02 and icms_diff < 0.02 and bc_diff < 0.02:
                            xml_item = x_item
                            already_matched.add(i)
                            break
                            
                # 4. Match by exact values only (Value + ICMS)
                if not xml_item:
                    for i, x_item in enumerate(xml_items_list):
                        if i in already_matched:
                            continue
                        val_diff = abs(sped_val - x_item.get("vl_prod_net", x_item["vl_item"]))
                        icms_diff = abs(sped_icms - x_item["vl_icms"])
                        if val_diff < 0.02 and icms_diff < 0.02:
                            xml_item = x_item
                            already_matched.add(i)
                            break

                # 5. Match by closest item value only (tolerance R$ 0.05)
                if not xml_item:
                    best_i = -1
                    best_diff = 999999.0
                    for i, x_item in enumerate(xml_items_list):
                        if i in already_matched:
                            continue
                        diff = abs(sped_val - x_item.get("vl_prod_net", x_item["vl_item"]))
                        if diff < 0.05 and diff < best_diff:
                            best_diff = diff
                            best_i = i
                    if best_i != -1:
                        xml_item = xml_items_list[best_i]
                        already_matched.add(best_i)

                # 6. Fallback to first unmatched item
                if not xml_item:
                    for i, x_item in enumerate(xml_items_list):
                        if i not in already_matched:
                            xml_item = x_item
                            already_matched.add(i)
                            break
                            
                if not xml_item:
                    continue
                    
                sped_cfop = item["cfop"]
                xml_cfop = xml_item["cfop"]
                prod_code = xml_item["cod_item"]
                prod_descr = xml_item["descr"]
                supplier_cnpj = xml_data["emit_id"]
                
                used_cfops.add(sped_cfop)
                allow_credit = icms_rules_map.get(sped_cfop, 1)
                if company_cnpj == "50421644000121" and sped_cfop in ("1949", "2949"):
                    allow_credit = 0
                
                xml_icms = xml_item["vl_icms"]
                sped_icms = item["vl_icms"]
                
                if allow_credit == 0:
                    if sped_icms > 0.01:
                        icms_issues.append({
                            "key": key,
                            "num": c100["num"],
                            "emit_name": xml_data.get("emit_name", participants.get(c100["emit"], c100["emit"])),
                            "num_item": n_item,
                            "descr": prod_descr,
                            "cfop": sped_cfop,
                            "reason": f"CFOP {sped_cfop} não permite crédito de ICMS, mas SPED creditou R$ {sped_icms:.2f}",
                            "sped_icms": sped_icms,
                            "xml_icms": xml_icms
                        })
                else:
                    if abs(sped_icms - xml_icms) > 0.02:
                        icms_issues.append({
                            "key": key,
                            "num": c100["num"],
                            "emit_name": xml_data.get("emit_name", participants.get(c100["emit"], c100["emit"])),
                            "num_item": n_item,
                            "descr": prod_descr,
                            "cfop": sped_cfop,
                            "reason": f"Divergência de crédito de ICMS: SPED = R$ {sped_icms:.2f}, XML = R$ {xml_icms:.2f}",
                            "sped_icms": sped_icms,
                            "xml_icms": xml_icms
                        })
                        
                mapping_key = (supplier_cnpj, xml_cfop, prod_code)
                mapped_cfop = cfop_mappings.get(mapping_key)
                
                # Check for Difal Interno
                if sped_cfop in ["1556", "1551", "1910"] or mapped_cfop in ["1556", "1551", "1910"]:
                    eff_pct = (xml_item["vl_icms"] / xml_item["vl_item"] * 100) if xml_item["vl_item"] > 0 else 0.0
                    difal_interno.append({
                        "emit_cnpj": supplier_cnpj,
                        "num_nf": c100["num"],
                        "descr": prod_descr,
                        "ncm": xml_item["ncm"],
                        "cest": xml_item["cest"],
                        "vl_item": xml_item["vl_item"],
                        "vl_bc": xml_item["vl_bc"],
                        "p_icms": xml_item["p_icms"],
                        "vl_icms": xml_item["vl_icms"],
                        "c_benef": xml_item["c_benef"],
                        "cfop_sped": sped_cfop,
                        "eff_pct": eff_pct
                    })
                    
                # Check for Difal Interestadual
                elif sped_cfop in ["2556", "2910", "2551", "2949"] or mapped_cfop in ["2556", "2910", "2551", "2949"]:
                    difal_interestadual.append({
                        "emit_cnpj": supplier_cnpj,
                        "emit_uf": xml_data.get("emit_uf", ""),
                        "num_nf": c100["num"],
                        "descr": prod_descr,
                        "ncm": xml_item["ncm"],
                        "cest": xml_item["cest"],
                        "vl_item": xml_item["vl_item"],
                        "vl_bc": xml_item["vl_bc"],
                        "p_icms": xml_item["p_icms"],
                        "vl_icms": xml_item["vl_icms"],
                        "vl_icms_st": xml_item["vl_icms_st"],
                        "cfop_sped": sped_cfop
                    })
                
                if mapped_cfop:
                    if sped_cfop != mapped_cfop:
                        cfop_classifications_issues.append({
                            "key": key,
                            "num": c100["num"],
                            "emit_name": xml_data.get("emit_name", participants.get(c100["emit"], c100["emit"])),
                            "num_item": n_item,
                            "product_code": prod_code,
                            "product_description": prod_descr,
                            "supplier_cfop": xml_cfop,
                            "sped_cfop": sped_cfop,
                            "expected_cfop": mapped_cfop
                        })
                else:
                    unmapped_key = (supplier_cnpj, xml_cfop, prod_code)
                    if unmapped_key not in seen_unmapped:
                        seen_unmapped.add(unmapped_key)
                        unmapped_items.append({
                            "company_cnpj": company_cnpj,
                            "supplier_cnpj": supplier_cnpj,
                            "supplier_cfop": xml_cfop,
                            "product_code": prod_code,
                            "product_description": prod_descr,
                            "current_sped_cfop": sped_cfop
                        })

        # 2. AUDIT D100 entries (CT-e)
        for key, d100 in sped_data["d100"].items():
            if d100["mod"] != "57":
                continue
                
            xml_path = cte_xmls.get(key)
            if not xml_path:
                missing_xmls.append({
                    "type": "CT-e",
                    "key": key,
                    "num": d100["num"],
                    "mod": d100["mod"],
                    "ind_emit": d100.get("ind_emit", ""),
                    "emit": participants.get(d100["emit"], d100["emit"]),
                    "vl_doc": d100["vl_doc"]
                })
                continue
                
            xml_data = parse_cte_xml(xml_path)
            if "error" in xml_data:
                continue
                
            diff_doc = abs(d100["vl_doc"] - xml_data["vl_doc"])
            if diff_doc > 0.02:
                value_discrepancies.append({
                    "type": "CT-e",
                    "key": key,
                    "num": d100["num"],
                    "emit_name": xml_data.get("emit_name", participants.get(d100["emit"], d100["emit"])),
                    "sped_val": d100["vl_doc"],
                    "xml_val": xml_data["vl_doc"],
                    "diff": diff_doc
                })
                
            xml_toma = xml_data["tomador_id"]
            if xml_toma and xml_toma != company_cnpj:
                cte_tomador_issues.append({
                    "key": key,
                    "num": d100["num"],
                    "emit_name": xml_data["emit_name"],
                    "tomador_id": xml_toma,
                    "tomador_role": xml_data["tomador_role"].upper(),
                    "company_cnpj": company_cnpj
                })

            # CT-e CFOP Audit
            xml_cfop = xml_data["cfop"]
            emit_uf = xml_data["emit_uf"]
            
            # Check mappings
            mapping_key = (xml_cfop, emit_uf)
            expected_cfop = cte_cfop_mappings.get(mapping_key)
            
            # In SPED, this CT-e might have one or more CFOPs registered in D190
            sped_cfops = d100.get("cfops", [])
            for sped_cfop in sped_cfops:
                used_cfops.add(sped_cfop) # For ICMS rules warning
                
                if expected_cfop:
                    if sped_cfop != expected_cfop:
                        cte_cfop_classifications_issues.append({
                            "key": key,
                            "num": d100["num"],
                            "emit_name": xml_data["emit_name"],
                            "emit_uf": emit_uf,
                            "supplier_cfop": xml_cfop,
                            "sped_cfop": sped_cfop,
                            "expected_cfop": expected_cfop
                        })
                else:
                    unmapped_cte_key = (xml_cfop, emit_uf)
                    if unmapped_cte_key not in seen_unmapped_cte:
                        seen_unmapped_cte.add(unmapped_cte_key)
                        unmapped_cte_items.append({
                            "company_cnpj": company_cnpj,
                            "supplier_cfop": xml_cfop,
                            "emitter_uf": emit_uf,
                            "current_sped_cfop": sped_cfop
                        })

        # 3. COMPILATION of all CT-e XMLs (General CT-e Report)
        if os.path.isdir(CTE_DIR):
            for f in os.listdir(CTE_DIR):
                if f.endswith(".xml"):
                    xml_path = os.path.join(CTE_DIR, f)
                    xml_data = parse_cte_xml(xml_path)
                    if "error" not in xml_data:
                        # Match transported NF-e keys with SPED to find registered CFOP
                        sped_cfops = []
                        for k in xml_data["nfe_keys"]:
                            if k in sped_data["c100"]:
                                for item in sped_data["c100"][k]["items"]:
                                    sped_cfops.append(item["cfop"])
                                    
                        sped_cfops_str = ", ".join(sorted(list(set(sped_cfops)))) if sped_cfops else "Não Localizado"
                        
                        cte_xml_list.append({
                            "num": xml_data["num"],
                            "emit_cnpj": xml_data["emit_id"],
                            "emit_uf": xml_data["emit_uf"],
                            "rem_name": xml_data["rem_name"],
                            "dest_name": xml_data["dest_name"],
                            "toma_name": xml_data["tomador_name"],
                            "nfe_nums": ", ".join(xml_data["nfe_nums"]) if xml_data["nfe_nums"] else "N/A",
                            "nfe_keys": ", ".join(xml_data["nfe_keys"]) if xml_data["nfe_keys"] else "N/A",
                            "nfe_cfop_sped": sped_cfops_str,
                            "vl_doc": xml_data["vl_doc"],
                            "vl_bc": xml_data["vl_bc"],
                            "vl_icms": xml_data["vl_icms"]
                        })

        # 4. COMPILATION of all NF-e XMLs (General NF-e Report)
        for key, xml_path in entrada_xmls.items():
            xml_data = parse_nfe_xml(xml_path)
            if "error" not in xml_data:
                emit_cnpj = xml_data["emit_id"]
                emit_name = xml_data["emit_name"]
                num_nf = xml_data["num"]
                
                for item in xml_data["items"]:
                    # Match item in SPED C170 to find registered CFOP
                    sped_cfop = "Não Localizado"
                    if key in sped_data["c100"]:
                        for sp_item in sped_data["c100"][key]["items"]:
                            if sp_item["num_item"] == item["num_item"]:
                                sped_cfop = sp_item["cfop"]
                                break
                                
                    nfe_xml_list.append({
                        "num_nf": num_nf,
                        "emit_cnpj": emit_cnpj,
                        "emit_name": emit_name,
                        "descr": item["descr"],
                        "cfop_xml": item["cfop"],
                        "cfop_sped": sped_cfop,
                        "vl_item": item["vl_item"],
                        "vl_bc": item["vl_bc"],
                        "vl_icms": item["vl_icms"],
                        "vl_icms_st": item["vl_icms_st"],
                        "fcp_st": item["fcp_st"],
                        "v_ipi": item["v_ipi"],
                        "v_ipi_devol": item["v_ipi_devol"]
                    })

        # Identify unconfigured ICMS CFOPs
        unmapped_icms_cfops = sorted(list(used_cfops - set(icms_rules_map.keys())))

        return jsonify({
            "status": "success",
            "company_name": sped_data["name"],
            "company_cnpj": company_cnpj,
            "total_sped_nfe": total_sped_nfe,
            "total_sped_cte": total_sped_cte,
            "missing_xmls": missing_xmls,
            "value_discrepancies": value_discrepancies,
            "icms_issues": icms_issues,
            "icms_st_notes": icms_st_notes,
            "cte_tomador_issues": cte_tomador_issues,
            "cfop_classifications_issues": cfop_classifications_issues,
            "cte_cfop_classifications_issues": cte_cfop_classifications_issues,
            "unmapped_items": unmapped_items,
            "unmapped_cte_items": unmapped_cte_items,
            "unmapped_icms_cfops": unmapped_icms_cfops,
            "difal_interno": difal_interno,
            "difal_interestadual": difal_interestadual,
            "cte_xml_list": cte_xml_list,
            "nfe_xml_list": nfe_xml_list
        })
        
    except Exception as e:
        import traceback
        return jsonify({"status": "error", "message": str(e), "trace": traceback.format_exc()}), 500

if __name__ == "__main__":
    load_config()
    init_db()
    print(f"Servidor Iniciado! Acesse http://localhost:5000 no seu navegador.")
    app.run(host="127.0.0.1", port=5000, debug=True)
