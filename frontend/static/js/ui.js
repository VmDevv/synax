/* Comportamentos de interface que valem para TODAS as telas.
 *
 * Nada aqui decide regra fiscal nem fala com o servidor: é só como a tela
 * responde. Fica fora do app.js (legado, com estado da auditoria antiga) e é
 * carregado no <head> com `defer` — o <body> tem hx-boost, e tudo que mora no
 * body é trocado a cada navegação. Por isso os ouvintes vão no `document` e os
 * elementos fixos (barra de progresso, voltar ao topo) são pendurados no
 * <html>, fora do alcance da troca.
 */
(function () {
    'use strict';

    // ------------------------------------------------------------------
    // Barra de carregamento no topo
    // ------------------------------------------------------------------
    // Toda requisição do HTMX acende a barra, MENOS as de polling ("every
    // 1.5s"): a tela de processamento pergunta o status sem parar, e a barra
    // piscando o tempo todo diria "carregando" sobre uma tela que está só
    // esperando.
    var barra = document.createElement('div');
    barra.id = 'syn-progresso';
    barra.setAttribute('aria-hidden', 'true');
    document.documentElement.appendChild(barra);

    var emVoo = 0;
    var apagar = null;

    function ehPolling(elt) {
        var gatilho = elt && elt.getAttribute && elt.getAttribute('hx-trigger');
        return !!(gatilho && gatilho.indexOf('every') !== -1);
    }

    document.addEventListener('htmx:beforeRequest', function (e) {
        if (ehPolling(e.detail && e.detail.elt)) return;
        emVoo++;
        clearTimeout(apagar);
        barra.classList.remove('syn-fim');
        barra.classList.add('syn-ativo');
    });

    document.addEventListener('htmx:afterRequest', function (e) {
        if (ehPolling(e.detail && e.detail.elt)) return;
        emVoo = Math.max(0, emVoo - 1);
        if (emVoo) return;
        barra.classList.add('syn-fim');
        apagar = setTimeout(function () {
            barra.classList.remove('syn-ativo', 'syn-fim');
        }, 350);
    });

    // ------------------------------------------------------------------
    // Voltar ao topo — os relatórios passam de mil linhas
    // ------------------------------------------------------------------
    var topo = document.createElement('button');
    topo.type = 'button';
    topo.id = 'syn-topo';
    topo.title = 'Voltar ao topo';
    topo.setAttribute('aria-label', 'Voltar ao topo');
    topo.innerHTML = '<i class="fa-solid fa-arrow-up"></i>';
    topo.addEventListener('click', function () {
        window.scrollTo({ top: 0, behavior: 'smooth' });
    });
    document.documentElement.appendChild(topo);

    var rolando = false;
    window.addEventListener('scroll', function () {
        if (rolando) return;
        rolando = true;
        requestAnimationFrame(function () {
            topo.classList.toggle('syn-visivel', window.scrollY > 900);
            rolando = false;
        });
    }, { passive: true });

    // ------------------------------------------------------------------
    // O título da aba conta o estado da auditoria
    // ------------------------------------------------------------------
    // A auditoria leva minutos, e quem espera vai fazer outra coisa em outra
    // aba. As telas marcam o estado com `data-syn-status`; aqui ele vira o
    // título. Sem marca nenhuma (tela de erro, por exemplo), o título volta
    // ao original — nunca fica dizendo "processando" sobre uma falha.
    var tituloOriginal = document.title;
    var TITULOS = {
        processando: '⏳ Processando auditoria… | Synax',
        aguardando: '● Confirme os itens da IA | Synax',
        concluido: '✓ Auditoria concluída | Synax'
    };

    function tituloNosso(t) {
        for (var k in TITULOS) if (TITULOS[k] === t) return true;
        return false;
    }

    document.addEventListener('htmx:afterSettle', function (e) {
        var alvo = e.detail && e.detail.target;
        if (!alvo || alvo.id !== 'process-status') return;
        // Navegação boostada troca o título da página; o "original" é sempre o
        // último que não foi escrito por aqui.
        if (!tituloNosso(document.title)) tituloOriginal = document.title;
        var marca = alvo.querySelector('[data-syn-status]');
        var estado = marca ? marca.getAttribute('data-syn-status') : null;
        document.title = TITULOS[estado] || tituloOriginal;
    });

    // ------------------------------------------------------------------
    // Cronômetro da tela de processamento
    // ------------------------------------------------------------------
    // O fragmento de polling é redesenhado a cada 1,5 s, então o início não
    // pode morar nele: vai para o sessionStorage, por tarefa. Sem storage
    // (janela privada bloqueada), o relógio conta a partir do que a tela viu.
    var inicios = {};

    function inicioDe(tarefa) {
        if (inicios[tarefa]) return inicios[tarefa];
        var chave = 'syn-inicio-' + tarefa;
        var guardado = null;
        try { guardado = parseInt(sessionStorage.getItem(chave), 10); } catch (err) { /* sem storage */ }
        var inicio = guardado || Date.now();
        try { sessionStorage.setItem(chave, String(inicio)); } catch (err) { /* sem storage */ }
        inicios[tarefa] = inicio;
        return inicio;
    }

    function formatarDuracao(ms) {
        var s = Math.max(0, Math.floor(ms / 1000));
        var m = Math.floor(s / 60);
        s = s % 60;
        return m + ':' + (s < 10 ? '0' : '') + s;
    }

    function tique() {
        var relogios = document.querySelectorAll('[data-syn-cronometro]');
        for (var i = 0; i < relogios.length; i++) {
            var el = relogios[i];
            el.textContent = formatarDuracao(Date.now() - inicioDe(el.getAttribute('data-syn-cronometro')));
        }
    }
    setInterval(tique, 1000);
    document.addEventListener('htmx:afterSettle', tique);

    // ------------------------------------------------------------------
    // Menus suspensos (<details class="syn-menu">)
    // ------------------------------------------------------------------
    // <details> abre e fecha sem JavaScript; o que falta é fechar ao clicar
    // fora e no Esc, como qualquer menu.
    document.addEventListener('click', function (e) {
        var abertos = document.querySelectorAll('details.syn-menu[open]');
        for (var i = 0; i < abertos.length; i++) {
            if (!abertos[i].contains(e.target)) abertos[i].removeAttribute('open');
        }
    });
    document.addEventListener('keydown', function (e) {
        if (e.key !== 'Escape') return;
        var abertos = document.querySelectorAll('details.syn-menu[open]');
        for (var i = 0; i < abertos.length; i++) {
            abertos[i].removeAttribute('open');
            var resumo = abertos[i].querySelector('summary');
            if (resumo) resumo.focus();
        }
    });

    // ------------------------------------------------------------------
    // Seletor com busca: <select data-busca>
    // ------------------------------------------------------------------
    // São 170 empresas num <select> nativo — achar uma era rolar a lista. O
    // <select> CONTINUA no formulário e continua sendo a fonte da verdade: é
    // ele que vai no POST, é nele que o `required` valida e é dele que sai o
    // `change` que o HTMX escuta. Esta camada só escolhe o valor por ele.
    //
    // As recentes aparecem no topo, mas NUNCA vêm pré-selecionadas: auditar a
    // empresa errada por um valor que ficou do mês passado é pior do que o
    // clique a mais.
    var MAX_RECENTES = 5;

    function semAcento(txt) {
        return (txt || '').normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase();
    }

    function soDigitos(txt) {
        return (txt || '').replace(/\D/g, '');
    }

    function formatarCnpj(d) {
        return d.length === 14
            ? d.replace(/^(\d{2})(\d{3})(\d{3})(\d{4})(\d{2})$/, '$1.$2.$3/$4-$5')
            : d;
    }

    function lerRecentes(chave) {
        try { return JSON.parse(localStorage.getItem('syn-recentes-' + chave) || '[]'); }
        catch (err) { return []; }
    }

    function gravarRecente(chave, valor) {
        if (!valor) return;
        var lista = lerRecentes(chave).filter(function (v) { return v !== valor; });
        lista.unshift(valor);
        try { localStorage.setItem('syn-recentes-' + chave, JSON.stringify(lista.slice(0, MAX_RECENTES))); }
        catch (err) { /* sem storage: só não lembra */ }
    }

    // Lê a opção do jeito que as telas a escrevem: "NOME (CNPJ) — Inativa".
    function descrever(opt) {
        var texto = (opt.textContent || '').trim();
        var inativa = /—\s*Inativa\s*$/i.test(texto);
        texto = texto.replace(/\s*—\s*Inativa\s*$/i, '');
        var nome = texto;
        var cnpj = '';
        var m = texto.match(/^(.*?)\s*\(([\d.\/-]{11,18})\)\s*$/);
        if (m) { nome = m[1]; cnpj = soDigitos(m[2]); }
        return {
            valor: opt.value,
            nome: nome,
            cnpj: cnpj,
            inativa: inativa,
            chave: semAcento(nome) + ' ' + cnpj
        };
    }

    var contador = 0;

    function montarBusca(select) {
        if (select.dataset.buscaPronta) return;
        select.dataset.buscaPronta = '1';
        contador++;

        var chaveRecentes = select.getAttribute('data-busca') || select.name || 'opcoes';
        var placeholder = select.getAttribute('data-busca-placeholder') || 'Buscar por nome ou CNPJ…';
        var idLista = 'syn-combo-lista-' + contador;

        var itens = [];
        var placeholderTexto = 'Selecione…';
        Array.prototype.forEach.call(select.options, function (opt) {
            // Opção desabilitada (ou sem valor) é o "Selecione…", não uma escolha.
            if (opt.disabled || !opt.value) { placeholderTexto = (opt.textContent || '').trim() || placeholderTexto; return; }
            itens.push(descrever(opt));
        });
        var porValor = {};
        itens.forEach(function (it) { porValor[it.valor] = it; });

        var caixa = document.createElement('div');
        // `data-busca-classe`: largura da caixa em telas onde o select ficava
        // numa linha de cabeçalho (o wrapper não herda a largura do select).
        caixa.className = 'syn-combo ' + (select.getAttribute('data-busca-classe') || '');
        select.parentNode.insertBefore(caixa, select);
        caixa.appendChild(select);
        select.classList.add('syn-combo-nativo');
        select.tabIndex = -1;
        select.setAttribute('aria-hidden', 'true');

        var botao = document.createElement('button');
        botao.type = 'button';
        botao.className = 'syn-combo-botao';
        botao.setAttribute('aria-haspopup', 'listbox');
        botao.setAttribute('aria-expanded', 'false');
        caixa.appendChild(botao);

        var painel = document.createElement('div');
        painel.className = 'syn-combo-painel';
        painel.hidden = true;
        painel.innerHTML =
            '<div class="syn-combo-busca"><i class="fa-solid fa-magnifying-glass"></i>' +
            '<input type="text" autocomplete="off" spellcheck="false" role="combobox" ' +
            'aria-autocomplete="list" aria-controls="' + idLista + '"></div>' +
            '<ul class="syn-combo-lista" role="listbox" id="' + idLista + '"></ul>' +
            '<div class="syn-combo-rodape"></div>';
        caixa.appendChild(painel);

        var campo = painel.querySelector('input');
        campo.placeholder = placeholder;
        var lista = painel.querySelector('ul');
        var rodape = painel.querySelector('.syn-combo-rodape');
        var visiveis = [];
        var destaque = -1;

        function pintarBotao() {
            var it = porValor[select.value];
            botao.innerHTML = '';
            var texto = document.createElement('span');
            texto.className = 'syn-combo-texto';
            if (it) {
                var nome = document.createElement('span');
                nome.className = 'syn-combo-nome';
                nome.textContent = it.nome;
                texto.appendChild(nome);
                if (it.cnpj) {
                    var doc = document.createElement('span');
                    doc.className = 'syn-combo-doc';
                    doc.textContent = formatarCnpj(it.cnpj);
                    texto.appendChild(doc);
                }
                if (it.inativa) {
                    var tag = document.createElement('span');
                    tag.className = 'syn-combo-tag';
                    tag.textContent = 'Inativa';
                    texto.appendChild(tag);
                }
            } else {
                var vazio = document.createElement('span');
                vazio.className = 'syn-combo-vazio';
                vazio.textContent = placeholderTexto;
                texto.appendChild(vazio);
            }
            botao.appendChild(texto);
            var seta = document.createElement('i');
            seta.className = 'fa-solid fa-chevron-down syn-combo-seta';
            botao.appendChild(seta);
            caixa.classList.toggle('syn-combo-escolhido', !!it);
        }

        function linha(it, i) {
            var li = document.createElement('li');
            li.setAttribute('role', 'option');
            li.id = idLista + '-' + i;
            li.className = 'syn-combo-opcao' + (it.valor === select.value ? ' syn-combo-atual' : '');
            li.dataset.valor = it.valor;
            var nome = document.createElement('span');
            nome.className = 'syn-combo-nome';
            nome.textContent = it.nome;
            li.appendChild(nome);
            if (it.inativa) {
                var tag = document.createElement('span');
                tag.className = 'syn-combo-tag';
                tag.textContent = 'Inativa';
                li.appendChild(tag);
            }
            if (it.cnpj) {
                var doc = document.createElement('span');
                doc.className = 'syn-combo-doc';
                doc.textContent = formatarCnpj(it.cnpj);
                li.appendChild(doc);
            }
            return li;
        }

        function titulo(texto) {
            var li = document.createElement('li');
            li.className = 'syn-combo-grupo';
            li.setAttribute('role', 'presentation');
            li.textContent = texto;
            return li;
        }

        function filtrar() {
            var termo = campo.value.trim();
            var q = semAcento(termo);
            var digitos = soDigitos(termo);
            lista.innerHTML = '';
            visiveis = [];

            var recentes = [];
            if (!termo) {
                recentes = lerRecentes(chaveRecentes)
                    .map(function (v) { return porValor[v]; })
                    .filter(Boolean);
            }
            var achados = itens.filter(function (it) {
                if (!termo) return true;
                if (it.chave.indexOf(q) !== -1) return true;
                return digitos.length >= 3 && it.cnpj.indexOf(digitos) !== -1;
            });

            if (recentes.length) {
                lista.appendChild(titulo('Usadas recentemente'));
                recentes.forEach(function (it) {
                    lista.appendChild(linha(it, visiveis.length));
                    visiveis.push(it);
                });
                lista.appendChild(titulo('Todas'));
            }
            achados.forEach(function (it) {
                lista.appendChild(linha(it, visiveis.length));
                visiveis.push(it);
            });

            if (!achados.length) {
                var nada = document.createElement('li');
                nada.className = 'syn-combo-nada';
                nada.textContent = 'Nada encontrado para "' + termo + '".';
                lista.appendChild(nada);
            }
            rodape.textContent = termo
                ? achados.length + ' de ' + itens.length
                : itens.length + ' opções · ↑↓ para navegar, Enter para escolher';
            destacar(visiveis.length ? 0 : -1);
        }

        function destacar(i) {
            var opcoes = lista.querySelectorAll('.syn-combo-opcao');
            if (destaque >= 0 && opcoes[destaque]) opcoes[destaque].classList.remove('syn-combo-foco');
            destaque = i;
            if (i >= 0 && opcoes[i]) {
                opcoes[i].classList.add('syn-combo-foco');
                opcoes[i].scrollIntoView({ block: 'nearest' });
                campo.setAttribute('aria-activedescendant', opcoes[i].id);
            } else {
                campo.removeAttribute('aria-activedescendant');
            }
        }

        function abrir() {
            if (!painel.hidden) return;
            painel.hidden = false;
            caixa.classList.add('syn-combo-aberto');
            botao.setAttribute('aria-expanded', 'true');
            campo.value = '';
            filtrar();
            campo.focus();
        }

        function fechar(devolverFoco) {
            if (painel.hidden) return;
            painel.hidden = true;
            caixa.classList.remove('syn-combo-aberto');
            botao.setAttribute('aria-expanded', 'false');
            if (devolverFoco) botao.focus();
        }

        function escolher(valor) {
            fechar(true);
            if (select.value === valor) return;
            select.value = valor;
            caixa.classList.remove('syn-combo-erro');
            // `change` com bubble: é o que o HTMX (hx-get no select) e os
            // formulários da tela escutam.
            select.dispatchEvent(new Event('change', { bubbles: true }));
        }

        botao.addEventListener('click', function () {
            if (painel.hidden) abrir(); else fechar(true);
        });
        botao.addEventListener('keydown', function (e) {
            if (e.key === 'ArrowDown' || e.key === 'ArrowUp') { e.preventDefault(); abrir(); }
        });
        campo.addEventListener('input', filtrar);
        campo.addEventListener('keydown', function (e) {
            if (e.key === 'ArrowDown') { e.preventDefault(); destacar(Math.min(destaque + 1, visiveis.length - 1)); }
            else if (e.key === 'ArrowUp') { e.preventDefault(); destacar(Math.max(destaque - 1, 0)); }
            else if (e.key === 'Enter') { e.preventDefault(); if (visiveis[destaque]) escolher(visiveis[destaque].valor); }
            else if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); fechar(true); }
            else if (e.key === 'Tab') { fechar(false); }
        });
        lista.addEventListener('mousedown', function (e) {
            // mousedown, e não click: o click viria depois do blur do campo.
            var li = e.target.closest('.syn-combo-opcao');
            if (!li) return;
            e.preventDefault();
            escolher(li.dataset.valor);
        });
        lista.addEventListener('mousemove', function (e) {
            var li = e.target.closest('.syn-combo-opcao');
            if (!li) return;
            var todas = Array.prototype.indexOf.call(lista.querySelectorAll('.syn-combo-opcao'), li);
            if (todas !== destaque) destacar(todas);
        });
        document.addEventListener('mousedown', function (e) {
            if (!caixa.contains(e.target)) fechar(false);
        });

        // Quem mudar o select por fora (reset do form, outro script) é
        // refletido aqui; e toda escolha entra nas recentes.
        select.addEventListener('change', function () {
            gravarRecente(chaveRecentes, select.value);
            if (select.value) caixa.classList.remove('syn-combo-erro');
            pintarBotao();
        });
        // `required` sem escolha: o navegador aponta para o select escondido,
        // e o contorno vermelho mostra QUAL campo faltou.
        select.addEventListener('invalid', function () {
            caixa.classList.add('syn-combo-erro');
        });
        var form = select.form;
        if (form) form.addEventListener('reset', function () { setTimeout(pintarBotao, 0); });

        pintarBotao();
    }

    function aplicar(raiz) {
        if (!raiz || !raiz.querySelectorAll) return;
        if (raiz.matches && raiz.matches('select[data-busca]')) montarBusca(raiz);
        var selects = raiz.querySelectorAll('select[data-busca]');
        for (var i = 0; i < selects.length; i++) montarBusca(selects[i]);
    }

    document.addEventListener('htmx:load', function (e) { aplicar(e.detail && e.detail.elt); });
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', function () { aplicar(document); });
    } else {
        aplicar(document);
    }
})();
