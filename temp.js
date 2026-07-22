
        function switchMainTab(tabId) {
            document.querySelectorAll('.tab-content').forEach(el => el.style.display = 'none');
            document.querySelectorAll('.nav-tab').forEach(el => el.classList.remove('active'));
            
            if (tabId === 'dashboard') {
                document.getElementById('dashboardSection').style.display = 'block';
                document.getElementById('tabBtnDashboard').classList.add('active');
            } else if (tabId === 'dutching') {
                document.getElementById('dutchingArenaSection').style.display = 'block';
                document.getElementById('tabBtnDutching').classList.add('active');
            } else if (tabId === 'keyvault') {
                document.getElementById('keyVaultSection').style.display = 'block';
                document.getElementById('tabBtnKeyVault').classList.add('active');
            }
        }

        let ws;
        let selectedTokenId = null;
        let selectedMarketTitle = "";

        const CustomDialog = {
            _modal: null,
            _title: null,
            _message: null,
            _icon: null,
            _promptContainer: null,
            _promptInput: null,
            _buttons: null,
            _resolve: null,

            init() {
                this._modal = document.getElementById("customDialogModal");
                this._title = document.getElementById("customDialogTitle");
                this._message = document.getElementById("customDialogMessage");
                this._icon = document.getElementById("customDialogIcon");
                this._promptContainer = document.getElementById("customDialogPromptContainer");
                this._promptInput = document.getElementById("customDialogPromptInput");
                this._buttons = document.getElementById("customDialogButtons");
            },

            show({ title, message, icon, type, defaultValue, placeholder }) {
                if (!this._modal) this.init();

                return new Promise((resolve) => {
                    this._resolve = resolve;
                    this._title.innerText = title || "Notification";
                    this._message.innerText = message || "";
                    this._icon.innerText = icon || "ℹ️";

                    if (type === "prompt") {
                        this._promptContainer.style.display = "block";
                        this._promptInput.value = defaultValue || "";
                        this._promptInput.placeholder = placeholder || "";
                        setTimeout(() => this._promptInput.focus(), 100);
                    } else {
                        this._promptContainer.style.display = "none";
                    }

                    // Dynamically build buttons based on type
                    this._buttons.innerHTML = "";
                    if (type === "alert") {
                        const btnOk = document.createElement("button");
                        btnOk.className = "btn btn-emerald";
                        btnOk.style.padding = "8px 24px";
                        btnOk.innerText = "OK";
                        btnOk.onclick = () => this.close(true);
                        this._buttons.appendChild(btnOk);
                    } else if (type === "confirm") {
                        const btnCancel = document.createElement("button");
                        btnCancel.className = "btn";
                        btnCancel.style.background = "transparent";
                        btnCancel.style.border = "1px solid rgba(255,255,255,0.1)";
                        btnCancel.style.padding = "8px 20px";
                        btnCancel.innerText = "Cancel";
                        btnCancel.onclick = () => this.close(false);

                        const btnOk = document.createElement("button");
                        btnOk.className = "btn btn-emerald";
                        btnOk.style.padding = "8px 24px";
                        btnOk.innerText = "Confirm";
                        btnOk.onclick = () => this.close(true);

                        this._buttons.appendChild(btnCancel);
                        this._buttons.appendChild(btnOk);
                    } else if (type === "prompt") {
                        const btnCancel = document.createElement("button");
                        btnCancel.className = "btn";
                        btnCancel.style.background = "transparent";
                        btnCancel.style.border = "1px solid rgba(255,255,255,0.1)";
                        btnCancel.style.padding = "8px 20px";
                        btnCancel.innerText = "Cancel";
                        btnCancel.onclick = () => this.close(null);

                        const btnOk = document.createElement("button");
                        btnOk.className = "btn btn-emerald";
                        btnOk.style.padding = "8px 24px";
                        btnOk.innerText = "Submit";
                        btnOk.onclick = () => this.close(this._promptInput.value);

                        this._buttons.appendChild(btnCancel);
                        this._buttons.appendChild(btnOk);

                        // Allow enter key to submit
                        this._promptInput.onkeydown = (e) => {
                            if (e.key === "Enter") {
                                this.close(this._promptInput.value);
                            }
                        };
                    }

                    this._modal.classList.add("active");
                });
            },

            close(result) {
                if (this._modal) {
                    this._modal.classList.remove("active");
                }
                if (this._resolve) {
                    this._resolve(result);
                    this._resolve = null;
                }
            },

            alert(message, title = "Notification", icon = "⚠️") {
                return this.show({ type: "alert", title, message, icon });
            },

            confirm(message, title = "Confirm Action", icon = "⚠️") {
                return this.show({ type: "confirm", title, message, icon });
            },

            prompt(message, defaultValue = "", title = "Input Required", icon = "✏️") {
                return this.show({ type: "prompt", title, message, icon, defaultValue });
            }
        };

        // API Token Management & Global Fetch Interceptor
        let apiToken = localStorage.getItem('poly_yield_token') || '';
        const originalFetch = window.fetch;
        window.fetch = async function(resource, config = {}) {
            if (!config.headers) {
                config.headers = {};
            }
            if (apiToken) {
                config.headers['Authorization'] = `Bearer ${apiToken}`;
            }
            
            let response = await originalFetch(resource, config);
            if (response.status === 401) {
                const tokenStr = await CustomDialog.prompt(
                    "Unauthorized (401). Please enter your PolyYield API Secret:",
                    "",
                    "Authentication Required",
                    "🔑"
                );
                if (tokenStr !== null) {
                    apiToken = tokenStr.trim();
                    localStorage.setItem('poly_yield_token', apiToken);
                    config.headers['Authorization'] = `Bearer ${apiToken}`;
                    response = await originalFetch(resource, config);
                }
            }
            return response;
        };

        // Establish WebSocket connection
        function connectWS() {
            const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
            ws = new WebSocket(`${proto}//${window.location.host}/ws`);

            ws.onmessage = function(event) {
                const msg = JSON.parse(event.data);
                handleWSMessage(msg);
            };

            ws.onclose = function() {
                setTimeout(connectWS, 2000); // Auto reconnect
            };
        }

        function handleWSMessage(msg) {
            if (msg.type === "welcome") {
                // Populate configuration in UI
                document.getElementById("modeSwitch").checked = msg.config["poly_yield.active_mode"] === "live";
                document.getElementById("engineSwitch").checked = msg.config["poly_yield.enabled"] === "true";
                document.getElementById("scanInterval").value = msg.config["poly_yield.scan_interval_s"] || 120;
                document.getElementById("drawdownLimit").value = msg.config["poly_yield.auto_exec_drawdown_limit"] || 50;
                document.getElementById("kellyFraction").value = msg.config["portfolio.kelly_fraction"] || 0.10;
                document.getElementById("kellyFractionVal").innerText = msg.config["portfolio.kelly_fraction"] || "0.10";
                document.getElementById("dailyLossLimit").value = msg.config["portfolio.daily_loss_limit"] || "50.0";
                document.getElementById("consecLossLimit").value = msg.config["portfolio.consecutive_loss_limit"] || "3";
                document.getElementById("circuitBreakerSwitch").checked = msg.config["portfolio.circuit_breaker_active"] === "true";
                
                updateChainIndicator(msg.config["polygon_chain_id"] || 137);
                updateStatsUI(msg.stats);
                buildStrategyToggles(msg.config);
                fetchPositions();
                syncStrategyManagerValues(msg.config);
            }
            else if (msg.type === "scan_complete") {
                fetchOpportunities();
                const statusEl = document.getElementById('scannerStatus');
                if (statusEl) statusEl.innerText = `● Scan #${msg.scan_count} — ${msg.opps_found} opps`;
            }
            else if (msg.type === "opportunity_update") {
                // Each scan broadcasts one update per opportunity — debounce so a scan
                // with 50 finds doesn't trigger 50 refetches
                debouncedFetchOpportunities();
            }
            else if (msg.type === "position_opened" || msg.type === "position_settled") {
                fetchPositions();
                fetchStats();
                logEvent(`[TRADE] Position status updated: ${msg.market || msg.pos_id}`);
            }
        }

        let activeExpandedOppId = null;

        let currentWalletBalance = 0;
        let activeOpportunities = [];
        let processedOppCache = [];
        let renderedOpps = [];   // opportunities currently rendered, indexed by row
        let currentSort = { col: null, dir: 'asc' };

        let _oppFetchTimer = null;
        function debouncedFetchOpportunities() {
            clearTimeout(_oppFetchTimer);
            _oppFetchTimer = setTimeout(fetchOpportunities, 1500);
        }

        function toggleSort(col) {
            if (currentSort.col === col) {
                currentSort.dir = currentSort.dir === 'asc' ? 'desc' : 'asc';
            } else {
                currentSort.col = col;
                currentSort.dir = 'desc'; // Default to desc for metrics
            }
            
            // Update sort icons UI
            ['prob', 'apy', 'size'].forEach(c => {
                const el = document.getElementById(`sort_${c}`);
                if (el) el.innerText = '↕';
            });
            const activeEl = document.getElementById(`sort_${col}`);
            if (activeEl) activeEl.innerText = currentSort.dir === 'asc' ? '▲' : '▼';

            filterOpportunities();
        }

        async function fetchWalletBalance() {
            try {
                const r = await fetch("/api/poly-yield/wallet-balance");
                const data = await r.json();
                currentWalletBalance = data.balance || 0;
                document.getElementById("walletBalance").innerText = `$${currentWalletBalance.toFixed(2)} USDC`;
                
                // Re-render opportunities with new wallet balance percentage
                if (activeOpportunities.length > 0) {
                    filterOpportunities();
                }
            } catch (e) {
                console.error(e);
            }
        }

        // Fetch Scanner Opportunities
        async function fetchOpportunities() {
            try {
                const r = await fetch("/api/poly-yield/opportunities");
                const data = await r.json();
                
                activeOpportunities = data.filter(o => o.status === 'open');
                
                // Pre-process for SOTA fast search filtering
                processedOppCache = activeOpportunities.map(opp => {
                    const strategyKey = opp.strategy || "";
                    const formattedName = formatStrategyName(strategyKey).toLowerCase();
                    // Maps keys to their common search terms
                    const searchAliases = {
                        's1_novelty': 's1 novelty novelty yield s1_novelty',
                        's2_split': 's2 split lp split share farm s2_split lp',
                        's3_buy_all': 's3 buy-all buy all arb s3_buy_all arbitrage',
                        's4_corr': 's4 corr arb correlation arb s4_corr correlation',
                        's5_sub_event': 's5 sub-event sub event arb s5_sub_event subevent',
                        's6_longshot': 's6 longshot longshot mm s6_longshot long shot',
                        'favorite_compounding': 'fav compound favorite compounding favorite_compounding compounding',
                        'copy_trading': 'copy trade copy trading copy_trading'
                    };
                    const searchStr = `${strategyKey.toLowerCase()} ${formattedName} ${searchAliases[strategyKey] || ""}`;

                    return {
                        id: opp.id,
                        strategy: formattedName,
                        strategySearch: searchStr,
                        risk: (opp.risk_level || "").toLowerCase(),
                        market: (opp.market_title || "").toLowerCase(),
                        outcome: (opp.outcome || "").toLowerCase(),
                        probStr: opp.implied_prob != null ? opp.implied_prob.toString().toLowerCase() + "%" : "n/a",
                        apyStr: opp.annualized_apy != null ? opp.annualized_apy.toString().toLowerCase() + "%" : "n/a",
                        probVal: opp.implied_prob || 0,
                        apyVal: opp.annualized_apy || 0,
                        sizeVal: opp.suggested_usdc || 0,
                        original: opp
                    };
                });
                
                filterOpportunities();
            } catch (e) {
                console.error(e);
            }
        }

        function filterOpportunities() {
            const tbody = document.getElementById("opportunitiesBody");
            
            if (processedOppCache.length === 0) {
                tbody.innerHTML = `<tr><td colspan="8" style="text-align: center; color: var(--text-secondary);">No opportunities found yet. Scanning...</td></tr>`;
                return;
            }

            const fStrategy = (document.getElementById("filterStrategy")?.value || "").toLowerCase();
            const fRisk = (document.getElementById("filterRisk")?.value || "").toLowerCase();
            const fMarket = (document.getElementById("filterMarket")?.value || "").toLowerCase();
            const fOutcome = (document.getElementById("filterOutcome")?.value || "").toLowerCase();
            const fProb = (document.getElementById("filterProb")?.value || "").toLowerCase();
            const fAPY = (document.getElementById("filterAPY")?.value || "").toLowerCase();

            // Very fast filter pass
            let filtered = processedOppCache.filter(item => {
                if (lastConfigData && lastConfigData[`${item.original.strategy}.enabled`] === 'false') {
                    return false;
                }
                return (!fStrategy || item.strategySearch.includes(fStrategy)) &&
                       (!fRisk || item.risk === fRisk) &&
                       (!fMarket || item.market.includes(fMarket)) &&
                       (!fOutcome || item.outcome.includes(fOutcome)) &&
                       (!fProb || item.probStr.includes(fProb)) &&
                       (!fAPY || item.apyStr.includes(fAPY));
            });

            // Fast sort pass
            if (currentSort.col) {
                const dir = currentSort.dir === 'asc' ? 1 : -1;
                filtered.sort((a, b) => {
                    let valA = a[currentSort.col + 'Val'];
                    let valB = b[currentSort.col + 'Val'];
                    return (valA - valB) * dir;
                });
            }

            if (filtered.length === 0) {
                tbody.innerHTML = `<tr><td colspan="8" style="text-align: center; color: var(--text-secondary);">No matching opportunities.</td></tr>`;
                return;
            }

            renderedOpps = filtered.map(item => item.original);

            tbody.innerHTML = filtered.map((item, idx) => {
                const opp = item.original;
                let sizeText = `$${opp.suggested_usdc || '0'}`;
                if (currentWalletBalance > 0 && opp.suggested_usdc) {
                    const pct = ((opp.suggested_usdc / currentWalletBalance) * 100).toFixed(1);
                    sizeText += ` <br><span style="font-size:10px; color:var(--text-secondary);">(${pct}%)</span>`;
                }
                const isManual = opp.exec_mode === 'manual';
                // NOTE: all interactive handlers are index-based lookups into renderedOpps —
                // inlining titles/ids into onclick attributes breaks on quotes/apostrophes.
                const execBtn = isManual
                    ? `<button class="btn" disabled title="This strategy is in MANUAL mode: follow the instructions, or switch it to semi/auto in the Bot Strategy Manager." style="padding:6px 12px; font-size:11px; opacity:0.45; cursor:not-allowed;">Manual Only</button>`
                    : `<button class="btn btn-emerald" id="exec_${idx}" onclick="executeTradeByIdx(${idx}, event)" style="padding:6px 12px; font-size:11px;">Execute</button>`;
                return `
                    <tr onclick="toggleOppDetailsByIdx(${idx})" style="cursor:pointer;">
                        <td><span class="badge ${getRiskBadge(opp.risk_level)}">${formatStrategyName(opp.strategy)}</span></td>
                        <td style="max-width:300px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;" title="${escapeHtml(opp.market_title || '')}">${escapeHtml(opp.market_title || '')}</td>
                        <td><span style="color:var(--cyber-emerald); font-weight:600;">${escapeHtml(opp.outcome || '')}</span></td>
                        <td>${opp.implied_prob != null ? opp.implied_prob + '%' : 'N/A'}</td>
                        <td><span style="color:var(--cyber-emerald); font-weight:600;">${opp.annualized_apy != null ? opp.annualized_apy + '%' : 'N/A'}</span>${opp.days_to_expiry != null && opp.days_to_expiry < 7 ? '<br><span style="font-size:10px;color:var(--alert-orange)">short-term</span>' : ''}</td>
                        <td>${sizeText}</td>
                        <td>${formatPayoff(opp.max_profit_usdc, opp.max_loss_usdc)}</td>
                        <td>${execBtn}</td>
                    </tr>
                    <tr id="details_${idx}" data-opp-id="${escapeHtml(opp.id || '')}" style="display:none; background: rgba(255,255,255,0.015);">
                        <td colspan="8" style="padding: 16px 24px; border-bottom: 1px solid rgba(255, 255, 255, 0.05);">
                            <div style="display:flex; justify-content:space-between; gap:40px; align-items:center;">
                                <div style="display:flex; flex-direction:column; gap:8px; font-size:12px; flex:1;">
                                    <div style="display:flex; align-items:center; gap:8px;">
                                        <span style="color:var(--text-secondary); width:80px; font-weight:600;">Market ID:</span>
                                        <code style="background:rgba(255,255,255,0.04); padding:2px 8px; border-radius:4px; color:#b3c5ff; font-family:monospace; font-size:11px;">${escapeHtml(opp.market_id || 'N/A')}</code>
                                        <button class="btn" onclick="copyOppField(${idx}, 'market_id', event)" style="padding:2px 6px; font-size:10px; background:rgba(255,255,255,0.05);">Copy</button>
                                    </div>
                                    <div style="display:flex; align-items:center; gap:8px;">
                                        <span style="color:var(--text-secondary); width:80px; font-weight:600;">Token ID:</span>
                                        <code style="background:rgba(255,255,255,0.04); padding:2px 8px; border-radius:4px; color:#b3c5ff; font-family:monospace; font-size:11px;">${escapeHtml(opp.token_id || 'N/A')}</code>
                                        <button class="btn" onclick="copyOppField(${idx}, 'token_id', event)" style="padding:2px 6px; font-size:10px; background:rgba(255,255,255,0.05);">Copy</button>
                                    </div>
                                    <div style="display:flex; align-items:center; gap:8px;">
                                        <span style="color:var(--text-secondary); width:80px; font-weight:600;">Market Link:</span>
                                        <a href="${escapeHtml(opp.market_url || ('https://polymarket.com/market/' + (opp.market_id || '')))}" target="_blank" style="color:var(--cyber-emerald); text-decoration:none;" onclick="event.stopPropagation();">Open in Polymarket ↗</a>
                                    </div>
                                </div>
                                <div style="display:flex; flex-direction:column; gap:8px; align-items:flex-end;">
                                    <button class="btn btn-emerald" onclick="autofillFromIdx(${idx}, event)" style="padding:6px 12px; font-size:11px;">
                                        ⚡ Auto-Fill Manual Trade Form
                                    </button>
                                </div>
                            </div>
                        </td>
                    </tr>
                `;
            }).join("");

            // Restore expanded state if still exists in the data
            if (activeExpandedOppId) {
                const el = document.querySelector(`[data-opp-id="${CSS.escape(activeExpandedOppId)}"]`);
                if (el) {
                    el.style.display = "table-row";
                }
            }
        }

        function toggleOppDetailsByIdx(idx) {
            const opp = renderedOpps[idx];
            const el = document.getElementById(`details_${idx}`);
            if (!el || !opp) return;
            const isVisible = el.style.display !== "none";

            // Collapse all other detail rows first (to keep table clean)
            const detailRows = document.querySelectorAll("[id^='details_']");
            detailRows.forEach(row => { row.style.display = "none"; });

            // Toggle clicked row
            if (!isVisible) {
                el.style.display = "table-row";
                activeExpandedOppId = opp.id;
                loadOrderBook(opp.token_id, opp.market_title || '');
            } else {
                activeExpandedOppId = null;
            }
        }

        function copyText(text, event) {
            event.stopPropagation();
            if (!text) return;
            navigator.clipboard.writeText(String(text)).then(() => {
                const btn = event.target;
                const originalText = btn.innerText;
                btn.innerText = "Copied!";
                btn.style.background = "var(--cyber-emerald)";
                btn.style.color = "#0b0d19";
                setTimeout(() => {
                    btn.innerText = originalText;
                    btn.style.background = "";
                    btn.style.color = "";
                }, 1500);
            });
        }

        function copyOppField(idx, field, event) {
            const opp = renderedOpps[idx];
            copyText(opp ? (opp[field] || '') : '', event);
        }

        function autofillFromIdx(idx, event) {
            const opp = renderedOpps[idx];
            if (!opp) return;
            autofillManualTrade(opp.market_id || '', opp.token_id || '', opp.market_title || '', opp.outcome || 'YES', opp.entry_price || 0.50, event);
        }

        function autofillManualTrade(marketId, tokenId, marketTitle, outcome, price, event) {
            event.stopPropagation();

            document.getElementById("manualMarketId").value = marketId;
            document.getElementById("manualTokenId").value = tokenId && tokenId !== 'None' ? tokenId : '';
            document.getElementById("manualMarketTitle").value = marketTitle;

            const outcomeSelect = document.getElementById("manualOutcome");
            if (outcomeSelect) {
                const upperOutcome = outcome.toUpperCase();
                if (upperOutcome === "YES" || upperOutcome === "NO") {
                    outcomeSelect.value = upperOutcome;
                } else {
                    outcomeSelect.value = upperOutcome.includes("YES") ? "YES" : "NO";
                }
            }

            document.getElementById("manualPrice").value = price || 0.50;
            
            logEvent(`[SYSTEM] Auto-filled manual trade form for: "${marketTitle}"`);
            
            const manualPanel = document.getElementById("manualMarketId").closest(".glass-panel");
            if (manualPanel) {
                manualPanel.scrollIntoView({ behavior: "smooth", block: "center" });
                manualPanel.style.borderColor = "var(--cyber-emerald)";
                manualPanel.style.boxShadow = "0 0 20px rgba(5, 255, 201, 0.3)";
                setTimeout(() => {
                    manualPanel.style.borderColor = "";
                    manualPanel.style.boxShadow = "";
                }, 2000);
            }
        }

        // Fetch Active Portfolio Positions
        async function fetchPositions() {
            try {
                const r = await fetch("/api/poly-yield/positions");
                const data = await r.json();
                const tbody = document.getElementById("positionsBody");

                if (data.length === 0) {
                    tbody.innerHTML = `<tr><td colspan="11" style="text-align: center; color: var(--text-secondary);">No active positions. Trigger a trade to start.</td></tr>`;
                    return;
                }

                tbody.innerHTML = data.map(pos => `
                    <tr>
                        <td><span class="badge ${getRiskBadge(pos.risk_level)}">${formatStrategyName(pos.strategy)}</span></td>
                        <td style="max-width:300px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;" title="${escapeHtml(pos.market_title || '')}">${escapeHtml(pos.market_title || '')}</td>
                        <td><span style="color:var(--cyber-emerald);">${escapeHtml(pos.outcome || '')}</span></td>
                        <td>$${(pos.cost_usdc || 0).toFixed(2)}</td>
                        <td>$${(pos.entry_price || 0).toFixed(4)}</td>
                        <td>${formatPayoff(pos.max_profit_usdc, pos.max_loss_usdc)}</td>
                        <td style="font-size:11px; white-space:nowrap;">${formatDateTime(pos.entry_at)}</td>
                        <td>${formatExecutedByBadge(pos.executed_by)}</td>
                        <td><span style="text-transform:uppercase; font-size:11px; font-weight:600; color: ${pos.mode==='live' ? 'var(--hot-pink)' : 'var(--text-secondary)'}">${pos.mode || 'paper'}</span></td>
                        <td><span style="color: ${pos.status === 'open' ? 'var(--alert-orange)' : pos.status === 'won' ? 'var(--cyber-emerald)' : 'var(--hot-pink)'}; font-weight:600; text-transform:capitalize;">${pos.status}${pos.realized_pnl != null && pos.status !== 'open' ? ' (' + (pos.realized_pnl >= 0 ? '+' : '') + '$' + pos.realized_pnl.toFixed(2) + ')' : ''}</span></td>
                        <td>
                            ${pos.status === 'open' ? `<button class="btn" style="background:linear-gradient(135deg, var(--hot-pink), #cc005f); padding:4px 8px; font-size:11px;" onclick="exitPositionPrompt('${pos.id}', ${pos.entry_price || 0.5})">Exit</button>` : `<span style="color:var(--text-secondary); font-size:11px;">—</span>`}
                        </td>
                    </tr>
                `).join("");
            } catch (e) {
                console.error(e);
            }
        }

        // Fetch Stats
        async function fetchStats() {
            try {
                const r = await fetch("/api/poly-yield/stats");
                const stats = await r.json();
                updateStatsUI(stats);
            } catch (e) {
                console.error(e);
            }
        }

        let lastKnownStats = [];

        function updateStatsUI(stats) {
            if (stats) lastKnownStats = stats;
            const dataToUse = lastKnownStats;
            
            const modeSwitch = document.getElementById("modeSwitch");
            const currentMode = modeSwitch && modeSwitch.checked ? "live" : "paper";

            let totalPnl = 0;
            let totalReturned = 0;
            let wins = 0;
            let losses = 0;
            let active = 0;

            if (dataToUse && dataToUse.length > 0) {
                dataToUse.forEach(s => {
                    if (s.mode !== currentMode) return;
                    totalPnl += s.total_pnl || 0;
                    totalReturned += s.total_returned || 0;
                    wins += s.win_count || 0;
                    losses += s.loss_count || 0;
                    active += s.open_positions || 0;
                });
            }

            const winRate = (wins + losses) > 0 ? (wins / (wins + losses)) * 100 : 0.0;

            document.getElementById("statTotalPnl").innerText = (totalPnl >= 0 ? "+" : "") + `$${totalPnl.toFixed(2)}`;
            document.getElementById("statTotalPnl").style.color = totalPnl >= 0 ? "var(--cyber-emerald)" : "var(--hot-pink)";
            document.getElementById("statWinRate").innerText = `${winRate.toFixed(1)}%`;
            document.getElementById("statActivePos").innerText = active;
            document.getElementById("statCompounded").innerText = (totalReturned >= 0 ? "+" : "") + `$${totalReturned.toFixed(2)}`;
            document.getElementById("statCompounded").style.color = totalReturned >= 0 ? "var(--cyber-emerald)" : "var(--hot-pink)";
            document.getElementById("statCompoundedSub").innerText = `${wins + losses} settled / ${active} open`;
        }

        // Order Book L2 Loader
        async function loadOrderBook(tokenId, marketTitle) {
            if (!tokenId || tokenId === "None" || tokenId === "null") return;
            selectedTokenId = tokenId;
            selectedMarketTitle = marketTitle;
            
            document.getElementById("orderbookPanel").style.display = "block";
            document.getElementById("obMarketTitle").innerText = `(${marketTitle})`;
            
            try {
                const r = await fetch(`/api/poly-yield/orderbook/${tokenId}`);
                const data = await r.json();
                
                const bidsBody = document.getElementById("obBidsBody");
                const asksBody = document.getElementById("obAsksBody");

                // Normalize to best-price-first: the CLOB API returns bids ascending and
                // asks descending (best price LAST), so an unsorted slice shows the worst levels
                const bids = (data.bids || []).slice().sort((a, b) => parseFloat(b.price) - parseFloat(a.price));
                const asks = (data.asks || []).slice().sort((a, b) => parseFloat(a.price) - parseFloat(b.price));

                bidsBody.innerHTML = bids.slice(0, 8).map(b => `
                    <div class="ob-row">
                        <div class="ob-bar bid" style="width: ${Math.min(100, (b.size/1000)*100)}%;"></div>
                        <span class="ob-val" style="color:var(--cyber-emerald); font-weight:600;">$${b.price}</span>
                        <span class="ob-val">${parseFloat(b.size).toFixed(0)}</span>
                    </div>
                `).join("") || `<div style="text-align:center; padding:20px; color:var(--text-secondary);">No Bids</div>`;

                asksBody.innerHTML = asks.slice(0, 8).map(a => `
                    <div class="ob-row">
                        <div class="ob-bar" style="width: ${Math.min(100, (a.size/1000)*100)}%;"></div>
                        <span class="ob-val" style="font-weight:600;">$${a.price}</span>
                        <span class="ob-val">${parseFloat(a.size).toFixed(0)}</span>
                    </div>
                `).join("") || `<div style="text-align:center; padding:20px; color:var(--text-secondary);">No Asks</div>`;

            } catch (e) {
                console.error(e);
            }
        }

        // Execute manually (semi-auto approval)
        async function executeTradeByIdx(idx, event) {
            event.stopPropagation();
            const opp = renderedOpps[idx];
            if (!opp) return;

            // Safety: explicit confirmation whenever real money is at stake
            const isLive = document.getElementById("modeSwitch")?.checked;
            if (isLive) {
                const ok = await CustomDialog.confirm(
                    `LIVE MODE — this will spend REAL USDC.\n\nMarket: ${opp.market_title || opp.id}\nOutcome: ${opp.outcome || ''}\nSize: $${opp.suggested_usdc || '?'} @ $${opp.entry_price || '?'}\n\nProceed?`,
                    'Confirm Live Trade',
                    '🚨'
                );
                if (!ok) return;
            }

            const btn = document.getElementById(`exec_${idx}`);
            if (btn) {
                btn.disabled = true;
                btn.innerText = 'Executing...';
                btn.style.opacity = '0.5';
            }
            logEvent(`[ENGINE] Manually executing opportunity ${opp.id}...`);
            try {
                const r = await fetch(`/api/poly-yield/execute/${encodeURIComponent(opp.id)}`, { method: "POST" });
                const res = await r.json();
                if (r.ok) {
                    logEvent(`[ENGINE] Execution SUCCESS: ${opp.id}`);
                    if (btn) { btn.innerText = '✓ Done'; btn.style.background = 'var(--cyber-emerald)'; }
                    fetchPositions();
                    fetchStats();
                    fetchWalletBalance();
                } else {
                    logEvent(`[ENGINE] Execution DENIED: ${res.detail || res.error || "Error"}`);
                    if (btn) { btn.innerText = 'Failed'; btn.style.background = 'var(--hot-pink)'; }
                    // Denials usually mean stale/moved prices — refresh the table
                    debouncedFetchOpportunities();
                }
            } catch (e) {
                logEvent(`[ENGINE] Execution failure: ${e}`);
                if (btn) { btn.innerText = 'Error'; btn.style.background = 'var(--hot-pink)'; }
            }
            // Re-enable after 3 seconds
            setTimeout(() => {
                if (btn) { btn.disabled = false; btn.innerText = 'Execute'; btn.style.opacity = '1'; btn.style.background = ''; }
            }, 3000);
        }

        // Dynamic config setters
        async function updateMode(el) {
            if (el.checked) {
                const confirmed = await CustomDialog.confirm(
                    'WARNING: Switching to LIVE MODE will use REAL MONEY from your wallet.\n\nAre you absolutely sure?',
                    'Live Mode Activation',
                    '🚨'
                );
                if (!confirmed) {
                    el.checked = false;
                    return;
                }
            }
            const mode = el.checked ? "live" : "paper";
            logEvent(`[SYSTEM] Switched active mode to: ${mode.toUpperCase()}`);
            await fetch("/api/poly-yield/config", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ key: "poly_yield.active_mode", value: mode })
            });
            setTimeout(fetchWalletBalance, 500); // Refresh balance UI
            updateStatsUI(); // Immediately refresh stats for new mode
        }

        async function updateEngineState(el) {
            const state = el.checked ? "true" : "false";
            logEvent(`[SYSTEM] Engine enabled: ${state.toUpperCase()}`);
            await fetch("/api/poly-yield/config", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ key: "poly_yield.enabled", value: state })
            });
        }

        async function updateScanInterval(el) {
            const val = parseInt(el.value);
            logEvent(`[SYSTEM] Scan interval set to ${val}s`);
            await fetch("/api/poly-yield/config", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ key: "poly_yield.scan_interval_s", value: val.toString() })
            });
        }

        async function updateDrawdownLimit(el) {
            const val = parseFloat(el.value);
            logEvent(`[SYSTEM] Drawdown limit set to ${val}%`);
            await fetch("/api/poly-yield/config", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ key: "poly_yield.auto_exec_drawdown_limit", value: val.toString() })
            });
        }

        async function updateKellyFraction(el) {
            const val = parseFloat(el.value);
            document.getElementById("kellyFractionVal").innerText = val.toFixed(2);
            logEvent(`[SYSTEM] Kelly Sizing fraction set to ${val}`);
            await fetch("/api/poly-yield/config", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ key: "portfolio.kelly_fraction", value: val.toString() })
            });
        }

        async function updateDailyLossLimit(el) {
            const val = parseFloat(el.value);
            logEvent(`[SYSTEM] Daily loss limit set to $${val}`);
            await fetch("/api/poly-yield/config", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ key: "portfolio.daily_loss_limit", value: val.toString() })
            });
        }

        async function updateConsecLossLimit(el) {
            const val = parseInt(el.value);
            logEvent(`[SYSTEM] Consecutive loss limit set to ${val}`);
            await fetch("/api/poly-yield/config", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ key: "portfolio.consecutive_loss_limit", value: val.toString() })
            });
        }

        async function updateCircuitBreaker(el) {
            const state = el.checked ? "true" : "false";
            logEvent(`[SYSTEM] Circuit Breaker active: ${state.toUpperCase()}`);
            await fetch("/api/poly-yield/config", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ key: "portfolio.circuit_breaker_active", value: state })
            });
        }

        // Toggle Amoy / Mainnet chains
        async function toggleNetwork() {
            const currentText = document.getElementById("networkText").innerText;
            let targetChainId = 137;
            if (currentText.includes("MAINNET")) {
                targetChainId = 80002;
                const ok = await CustomDialog.confirm(
                    'Switch to AMOY TESTNET?\n\nNote: market data still comes from the mainnet Gamma API — testnet is only useful for testing CLOB order plumbing, NOT for validating strategies. Opportunities shown will not exist on testnet.',
                    'Testnet Warning',
                    '⚠️'
                );
                if (!ok) return;
            }

            logEvent(`[SYSTEM] Switched Polygon network to: Chain ${targetChainId}`);
            updateChainIndicator(targetChainId);
            
            await fetch("/api/poly-yield/config", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ key: "polygon_chain_id", value: targetChainId.toString() })
            });
        }

        function updateChainIndicator(chainId) {
            const text = document.getElementById("networkText");
            const dot = document.getElementById("networkDot");
            
            if (parseInt(chainId) === 80002) {
                text.innerText = "POLYGON AMOY TESTNET";
                text.style.color = "var(--alert-orange)";
                dot.className = "network-status-dot amoy";
            } else {
                text.innerText = "POLYGON MAINNET";
                text.style.color = "var(--cyber-emerald)";
                dot.className = "network-status-dot";
            }
        }

        // Credentials Modal controls
        function openKeyModal() {
            document.getElementById("keyModal").className = "modal active";
        }

        function closeKeyModal() {
            document.getElementById("keyModal").className = "modal";
        }

        async function submitKey() {
            const service = document.getElementById("keyService").value;
            const value = document.getElementById("keyValue").value;
            const label = document.getElementById("keyLabel").value;

            if (!value) return;

            logEvent(`[SYSTEM] Saving keys for service: ${service}...`);
            try {
                const r = await fetch("/api/poly-yield/keys", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ service, value, label })
                });
                if (r.ok) {
                    logEvent(`[SYSTEM] Encrypted key for ${service} stored successfully.`);
                    closeKeyModal();
                } else {
                    logEvent(`[SYSTEM] Key storage failed.`);
                }
            } catch (e) {
                console.error(e);
            }
        }

        // Helpers
        function logEvent(msg) {
            const panel = document.getElementById("logsPanel");
            const time = new Date().toLocaleTimeString();
            const entry = document.createElement('div');
            entry.textContent = `[${time}] ${msg}`;
            panel.appendChild(entry);
            // Cap at 100 entries to prevent memory bloat
            while (panel.children.length > 100) {
                panel.removeChild(panel.firstChild);
            }
            panel.scrollTop = panel.scrollHeight;
        }

        function getRiskBadge(level) {
            if (level === "Low") return "badge-low";
            if (level === "Medium") return "badge-med";
            return "badge-high";
        }

        // Renders a best-case/worst-case USDC payoff. When both numbers are equal
        // (arbitrage baskets: the payout is guaranteed regardless of outcome) shows a
        // single "Guaranteed" figure instead of a misleading range.
        function formatPayoff(maxProfit, maxLoss) {
            if (maxProfit == null || maxLoss == null) {
                return `<span style="color:var(--text-secondary); font-size:11px;">N/A</span>`;
            }
            const fmt = (v) => `${v >= 0 ? '+' : '-'}$${Math.abs(v).toFixed(2)}`;
            if (Math.abs(maxProfit - maxLoss) < 0.005) {
                return `<span style="color:var(--cyber-emerald); font-weight:600; font-size:12px;" title="Arbitrage: guaranteed payout regardless of outcome">🔒 ${fmt(maxProfit)}</span>`;
            }
            return `<span style="color:var(--cyber-emerald); font-size:12px;">▲ ${fmt(maxProfit)}</span><br><span style="color:var(--hot-pink); font-size:12px;">▼ ${fmt(maxLoss)}</span>`;
        }

        function formatExecutedByBadge(executedBy) {
            const isManual = executedBy === 'manual';
            const icon = isManual ? '🖐' : '🤖';
            const label = isManual ? 'Manual' : 'Bot';
            const color = isManual ? 'var(--alert-orange)' : 'var(--cyber-emerald)';
            return `<span style="font-size:11px; font-weight:600; color:${color};">${icon} ${label}</span>`;
        }

        function formatDateTime(ts) {
            if (!ts) return '—';
            // SQLite stores 'YYYY-MM-DD HH:MM:SS' in UTC; normalize so Date parses it as UTC
            const iso = ts.includes('T') ? ts : ts.replace(' ', 'T') + 'Z';
            const d = new Date(iso);
            if (isNaN(d.getTime())) return escapeHtml(ts);
            return d.toLocaleString(undefined, { year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit' });
        }

        function escapeHtml(text) {
            if (!text) return '';
            const map = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;' };
            return String(text).replace(/[&<>"']/g, function(m) { return map[m]; });
        }

        function formatStrategyName(key) {
            if (!key) return 'Unknown';
            const names = {
                's1_novelty': 'S1 Novelty',
                's2_split': 'S2 Split LP',
                's3_buy_all': 'S3 Buy-All',
                's4_corr': 'S4 Corr Arb',
                's5_sub_event': 'S5 Sub-Event',
                's6_longshot': 'S6 Longshot',
                's8_late_stage': 'S8 Late Stage',
                's9_stablecoin_peg': 'S9 Stable Peg',
                's10_oracle': 'S10 Oracle',
                's11_overreaction': 'S11 Overreact',
                's12_momentum': 'S12 Momentum',
                's13_sentiment': 'S13 Sentiment',
                's14_macro_corr': 'S14 Macro',
                's15_theta': 'S15 Theta',
                's16_poll_drift': 'S16 Poll Drift',
                's17_sniper': 'S17 Sniper',
                's18_straddle': 'S18 Straddle',
                's19_longshot_yes': 'S19 Longshot YES',
                'favorite_compounding': 'Fav Compound',
                'copy_trading': 'Copy Trade',
                'manual': 'Manual'
            };
            return names[key] || key;
        }

        function buildStrategyToggles(config) {
            const strategies = strategiesList;
            const container = document.getElementById('strategyToggles');
            if (!container) return;
            container.innerHTML = strategies.map(s => {
                const enabled = config && config[`${s.key}.enabled`] === 'true';
                return `
                    <div class="switch-container">
                        <span class="switch-label" style="font-size:13px;">${s.label}</span>
                        <label class="switch">
                            <input type="checkbox" ${enabled ? 'checked' : ''} onchange="toggleStrategy('${s.key}', this)">
                            <span class="slider"></span>
                        </label>
                    </div>`;
            }).join('');
        }

        async function toggleStrategy(key, el) {
            const val = el.checked ? 'true' : 'false';
            logEvent(`[SYSTEM] Strategy ${key} ${el.checked ? 'ENABLED' : 'DISABLED'}`);
            await fetch('/api/poly-yield/config', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ key: `${key}.enabled`, value: val })
            });
        }

        async function placeManualTrade() {
            const marketId = document.getElementById("manualMarketId").value.trim();
            const tokenId = document.getElementById("manualTokenId").value.trim() || null;
            const marketTitle = document.getElementById("manualMarketTitle").value.trim() || "Manual Trade";
            const outcome = document.getElementById("manualOutcome").value;
            const price = parseFloat(document.getElementById("manualPrice").value);
            const stake = parseFloat(document.getElementById("manualStake").value);
            
            const sl_val = document.getElementById("manualSL").value.trim();
            const tp_val = document.getElementById("manualTP").value.trim();
            const ts_val = document.getElementById("manualTS").value.trim();

            const stop_loss_price = sl_val ? parseFloat(sl_val) : null;
            const take_profit_price = tp_val ? parseFloat(tp_val) : null;
            const trailing_stop_pct = ts_val ? parseFloat(ts_val) : null;

            if (!marketId || isNaN(price) || isNaN(stake)) {
                await CustomDialog.alert("Please fill in Market ID, Price, and Stake.", "Missing Fields", "⚠️");
                return;
            }

            logEvent(`[SYSTEM] Placing manual trade for ${marketTitle}...`);
            try {
                const r = await fetch("/api/poly-yield/manual-trade", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        market_id: marketId,
                        outcome: outcome,
                        stake_usdc: stake,
                        price: price,
                        stop_loss_price: stop_loss_price,
                        take_profit_price: take_profit_price,
                        trailing_stop_pct: trailing_stop_pct,
                        token_id: tokenId,
                        market_title: marketTitle
                    })
                });
                const res = await r.json();
                if (r.ok) {
                    logEvent(`[SYSTEM] Manual trade placement SUCCESS: ${res.order_id || 'simulated'}`);
                    fetchPositions();
                    fetchStats();
                    fetchWalletBalance();
                    // Clear inputs
                    document.getElementById("manualMarketId").value = "";
                    document.getElementById("manualTokenId").value = "";
                    document.getElementById("manualSL").value = "";
                    document.getElementById("manualTP").value = "";
                    document.getElementById("manualTS").value = "";
                } else {
                    logEvent(`[SYSTEM] Manual trade placement DENIED: ${res.detail || "Error"}`);
                }
            } catch (e) {
                logEvent(`[SYSTEM] Manual trade failure: ${e}`);
            }
        }

        async function exitPositionPrompt(posId, entryPrice) {
            const exitPriceStr = await CustomDialog.prompt(
                `Enter exit price (USDC per share) for position:`,
                entryPrice.toFixed(2),
                "Exit Position",
                "💰"
            );
            if (exitPriceStr === null || exitPriceStr.trim() === "") return; // User cancelled
            
            const current_price = parseFloat(exitPriceStr);
            if (isNaN(current_price) || current_price <= 0) {
                await CustomDialog.alert("Please enter a valid positive number.", "Invalid Price", "❌");
                return;
            }

            logEvent(`[SYSTEM] Exiting position ${posId} at $${current_price}...`);
            try {
                const r = await fetch(`/api/poly-yield/exit/${posId}`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ current_price })
                });
                const res = await r.json();
                if (r.ok) {
                    logEvent(`[SYSTEM] Position exited successfully. Realized PnL: $${res.realized_pnl.toFixed(2)}`);
                    fetchPositions();
                    fetchStats();
                    fetchWalletBalance();
                } else {
                    logEvent(`[SYSTEM] Exit DENIED: ${res.detail || "Error"}`);
                }
            } catch (e) {
                logEvent(`[SYSTEM] Exit failure: ${e}`);
            }
        }

        // Paper wallet fund management
        async function paperDeposit() {
            const amountStr = await CustomDialog.prompt(
                'Enter amount to deposit into paper wallet (USDC):',
                '100',
                'Deposit Funds',
                '💵'
            );
            if (amountStr === null || amountStr.trim() === "") return;
            const amount = parseFloat(amountStr);
            if (isNaN(amount) || amount <= 0) {
                await CustomDialog.alert('Please enter a valid positive number.', 'Invalid Amount', '❌');
                return;
            }
            logEvent(`[SYSTEM] Depositing $${amount.toFixed(2)} to paper wallet...`);
            try {
                const r = await fetch('/api/poly-yield/paper-deposit', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ amount })
                });
                const res = await r.json();
                if (r.ok) {
                    logEvent(`[SYSTEM] Paper deposit SUCCESS. New balance: $${res.balance.toFixed(2)}`);
                    fetchWalletBalance();
                } else {
                    logEvent(`[SYSTEM] Paper deposit DENIED: ${res.detail || 'Error'}`);
                }
            } catch (e) {
                logEvent(`[SYSTEM] Paper deposit failure: ${e}`);
            }
        }

        async function paperReset() {
            const amountStr = await CustomDialog.prompt(
                'Reset paper wallet balance to (USDC):',
                '1000',
                'Reset Balance',
                '🔄'
            );
            if (amountStr === null || amountStr.trim() === "") return;
            const amount = parseFloat(amountStr);
            if (isNaN(amount) || amount < 0) {
                await CustomDialog.alert('Please enter a valid non-negative number.', 'Invalid Amount', '❌');
                return;
            }
            const confirmed = await CustomDialog.confirm(
                `This will reset your paper balance to $${amount.toFixed(2)}.\n\nAll existing paper positions will keep their current state but won't affect the new balance.`,
                'Reset Warning',
                '⚠️'
            );
            if (!confirmed) {
                return;
            }
            logEvent(`[SYSTEM] Resetting paper wallet to $${amount.toFixed(2)}...`);
            try {
                const r = await fetch('/api/poly-yield/paper-reset', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ amount })
                });
                const res = await r.json();
                if (r.ok) {
                    logEvent(`[SYSTEM] Paper wallet RESET to $${res.balance.toFixed(2)}`);
                    fetchWalletBalance();
                    fetchStats();
                } else {
                    logEvent(`[SYSTEM] Paper reset DENIED: ${res.detail || 'Error'}`);
                }
            } catch (e) {
                logEvent(`[SYSTEM] Paper reset failure: ${e}`);
            }
        }

        // Bot Strategy Manager configuration list
        const strategiesList = [
            {
                key: 's1_novelty',
                label: 'S1 Novelty Yield',
                params: [
                    { key: 'max_yes_price', label: 'Max YES Price', type: 'number', step: '0.01', unit: 'USDC' },
                    { key: 'min_apy', label: 'Min APY', type: 'number', step: '0.1', unit: '%' },
                    { key: 'max_position_pct', label: 'Max Position Size', type: 'number', step: '0.1', unit: '%', multiplier: 100 }
                ]
            },
            {
                key: 's2_split',
                label: 'S2 Split Share LP',
                params: [
                    { key: 'min_apy', label: 'Min APY', type: 'number', step: '0.1', unit: '%' },
                    { key: 'max_position_pct', label: 'Max Position Size', type: 'number', step: '0.1', unit: '%', multiplier: 100 }
                ]
            },
            {
                key: 's3_buy_all',
                label: 'S3 Buy-All Arb',
                params: [
                    { key: 'min_profit_pct', label: 'Min Profit', type: 'number', step: '0.1', unit: '%' },
                    { key: 'max_position_pct', label: 'Max Position Size', type: 'number', step: '0.1', unit: '%', multiplier: 100 }
                ]
            },
            {
                key: 's4_corr',
                label: 'S4 Correlation Arb',
                params: [
                    { key: 'min_gap_pct', label: 'Min Gap', type: 'number', step: '0.1', unit: '%' },
                    { key: 'max_position_pct', label: 'Max Position Size', type: 'number', step: '0.1', unit: '%', multiplier: 100 }
                ]
            },
            {
                key: 's5_sub_event',
                label: 'S5 Sub-Event Arb',
                params: [
                    { key: 'min_gap_pct', label: 'Min Gap', type: 'number', step: '0.1', unit: '%' },
                    { key: 'max_position_pct', label: 'Max Position Size', type: 'number', step: '0.1', unit: '%', multiplier: 100 }
                ]
            },
            {
                key: 's6_longshot',
                label: 'S6 Longshot MM',
                params: [
                    { key: 'max_yes_price', label: 'Max YES Price', type: 'number', step: '0.01', unit: 'USDC' },
                    { key: 'max_positions', label: 'Max Positions', type: 'number', step: '1', unit: 'cnt' },
                    { key: 'position_pct', label: 'Position Size', type: 'number', step: '0.1', unit: '%', multiplier: 100 }
                ]
            },
            {
                key: 'favorite_compounding',
                label: 'Favorite Compounding',
                params: [
                    { key: 'min_yes_price', label: 'Min YES Price', type: 'number', step: '0.01', unit: 'USDC' },
                    { key: 'max_days_left', label: 'Max Days Left', type: 'number', step: '0.5', unit: 'days' },
                    { key: 'max_position_pct', label: 'Max Position Size', type: 'number', step: '0.1', unit: '%', multiplier: 100 },
                    { key: 'min_apy', label: 'Min APY', type: 'number', step: '0.1', unit: '%' }
                ]
            },
            {
                key: 'copy_trading',
                label: 'Copy Trading',
                params: [
                    { key: 'max_position_pct', label: 'Max Position Size', type: 'number', step: '0.1', unit: '%', multiplier: 100 }
                ]
            },
            {
                key: 's8_late_stage',
                label: 'S8 Late Stage Yield',
                params: [
                    { key: 'min_price', label: 'Min Price', type: 'number', step: '0.005', unit: 'USDC' },
                    { key: 'max_days_left', label: 'Max Days Left', type: 'number', step: '0.5', unit: 'days' },
                    { key: 'max_position_pct', label: 'Max Position Size', type: 'number', step: '0.1', unit: '%', multiplier: 100 },
                    { key: 'min_apy', label: 'Min APY', type: 'number', step: '0.1', unit: '%' }
                ]
            },
            { key: 's9_stablecoin_peg', label: 'S9 Stablecoin Peg Arb', params: [] },
            { key: 's10_oracle', label: 'S10 Oracle Discrepancy', params: [] },
            { key: 's11_overreaction', label: 'S11 Overreaction Scalp', params: [] },
            { key: 's12_momentum', label: 'S12 Trend Momentum', params: [] },
            { key: 's13_sentiment', label: 'S13 Sentiment Tracker', params: [] },
            { key: 's14_macro_corr', label: 'S14 Macro Correlation', params: [] },
            { key: 's15_theta', label: 'S15 Theta Harvester', params: [] },
            { key: 's16_poll_drift', label: 'S16 Poll Drift', params: [] },
            { key: 's17_sniper', label: 'S17 Liquidity Sniper', params: [] },
            { key: 's18_straddle', label: 'S18 Catalyst Straddle', params: [] },
            { key: 's19_longshot_yes', label: 'S19 Longshot YES', params: [] }
        ];

        let isStrategyGridBuilt = false;
        let lastConfigData = null;

        function toggleBotManager() {
            const content = document.getElementById("botManagerContent");
            const arrow = document.getElementById("botManagerArrow");
            if (content.style.display === "none") {
                content.style.display = "block";
                arrow.style.transform = "rotate(180deg)";
                renderStrategyManagerGrid();
            } else {
                content.style.display = "none";
                arrow.style.transform = "rotate(0deg)";
            }
        }

        function renderStrategyManagerGrid() {
            if (isStrategyGridBuilt) return;
            const grid = document.getElementById("strategyManagerGrid");
            if (!grid) return;
            
            grid.innerHTML = strategiesList.map(s => {
                const paramInputs = s.params.map(p => {
                    const fullKey = `${s.key}.${p.key}`;
                    return `
                        <div class="param-field">
                            <span class="param-label">${p.label}</span>
                            <div class="param-input-container">
                                <input type="number" id="param_${fullKey}" class="control-input" step="${p.step}" onchange="saveStrategyParam('${fullKey}', this, ${p.multiplier || 1})" style="padding: 6px 10px; font-size: 12px;">
                                <span class="param-unit">${p.unit}</span>
                            </div>
                        </div>
                    `;
                }).join("");
                
                return `
                    <div class="strategy-card">
                        <div class="strategy-card-header">
                            <span class="strategy-card-title">${s.label}</span>
                            <label class="switch" style="width:38px; height:20px;">
                                <input type="checkbox" id="enable_${s.key}" onchange="toggleStrategyState('${s.key}', this)">
                                <span class="slider" style="border-radius:20px;"></span>
                            </label>
                        </div>
                        <div class="control-group" style="margin-bottom: 8px;">
                            <span class="param-label">Execution Mode</span>
                            <select id="mode_${s.key}" class="control-input" onchange="saveStrategyMode('${s.key}', this)" style="background:#1b203c; padding: 6px 10px; font-size: 12px; height: 28px; line-height: 12px; margin-top: 4px;">
                                <option value="manual">Manual (Read Only)</option>
                                <option value="semi">Semi-Auto (Approval Req)</option>
                                <option value="auto">Auto (Bot Executes)</option>
                            </select>
                        </div>
                        <div class="strategy-params-grid">
                            ${paramInputs}
                        </div>
                    </div>
                `;
            }).join("");
            
            isStrategyGridBuilt = true;
            
            if (lastConfigData) {
                syncStrategyManagerValues(lastConfigData);
            }
        }

        function syncStrategyManagerValues(config) {
            lastConfigData = config;
            if (!isStrategyGridBuilt) return;
            
            strategiesList.forEach(s => {
                const enableEl = document.getElementById(`enable_${s.key}`);
                if (enableEl) {
                    enableEl.checked = config[`${s.key}.enabled`] === 'true';
                }
                
                const modeEl = document.getElementById(`mode_${s.key}`);
                if (modeEl) {
                    modeEl.value = config[`${s.key}.exec_mode`] || 'manual';
                }
                
                s.params.forEach(p => {
                    const fullKey = `${s.key}.${p.key}`;
                    const el = document.getElementById(`param_${fullKey}`);
                    if (el && document.activeElement !== el) {
                        const rawVal = parseFloat(config[fullKey]);
                        if (!isNaN(rawVal)) {
                            const displayVal = p.multiplier ? rawVal * p.multiplier : rawVal;
                            el.value = Number(displayVal.toFixed(2));
                        } else {
                            el.value = "";
                        }
                    }
                });
            });
            
            // Re-run filter to ensure table reflects newly disabled/enabled strategies
            filterOpportunities();
        }

        async function toggleStrategyState(key, el) {
            const val = el.checked ? 'true' : 'false';
            logEvent(`[SYSTEM] Strategy ${key} ${el.checked ? 'ENABLED' : 'DISABLED'}`);
            await fetch('/api/poly-yield/config', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ key: `${key}.enabled`, value: val })
            });
        }

        async function saveStrategyMode(key, el) {
            const val = el.value;
            logEvent(`[SYSTEM] Strategy ${key} mode set to ${val.toUpperCase()}`);
            await fetch('/api/poly-yield/config', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ key: `${key}.exec_mode`, value: val })
            });
        }

        async function setGlobalStrategyMode(el) {
            const mode = el.value;
            if (!mode) return;
            
            const confirmed = await CustomDialog.confirm(
                `Are you sure you want to set ALL strategies to ${mode.toUpperCase()} mode?`,
                'Confirm Global Mode Change',
                '⚠️'
            );
            if (!confirmed) {
                el.value = "";
                return;
            }

            logEvent(`[SYSTEM] Setting all strategies to ${mode.toUpperCase()} mode...`);
            
            const promises = strategiesList.map(s => {
                return fetch('/api/poly-yield/config', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ key: `${s.key}.exec_mode`, value: mode })
                });
            });
            
            try {
                await Promise.all(promises);
                logEvent(`[SYSTEM] All strategies updated to ${mode.toUpperCase()} mode.`);
                el.value = ""; // Reset selector
            } catch (e) {
                logEvent(`[SYSTEM] Error updating global mode: ${e}`);
            }
        }

        async function saveStrategyParam(fullKey, el, multiplier) {
            const val = parseFloat(el.value);
            if (isNaN(val)) return;
            const dbVal = multiplier !== 1 ? val / multiplier : val;
            logEvent(`[SYSTEM] Strategy param ${fullKey} set to ${dbVal}`);
            await fetch('/api/poly-yield/config', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ key: fullKey, value: dbVal.toString() })
            });
        }

        // ---------------------------------------------------------------------
        // Trade History & Audit
        // ---------------------------------------------------------------------
        let isTradeHistoryLoaded = false;
        let rawTradeHistory = [];

        function toggleTradeHistory() {
            const content = document.getElementById("tradeHistoryContent");
            const arrow = document.getElementById("tradeHistoryArrow");
            if (content.style.display === "none") {
                content.style.display = "block";
                arrow.style.transform = "rotate(180deg)";
                if (!isTradeHistoryLoaded) {
                    isTradeHistoryLoaded = true;
                    fetchAccountingSummary();
                    fetchTradeHistory();
                }
            } else {
                content.style.display = "none";
                arrow.style.transform = "rotate(0deg)";
            }
        }

        function toggleWalletLedger() {
            const content = document.getElementById("walletLedgerContent");
            const arrow = document.getElementById("walletLedgerArrow");
            if (content.style.display === "none") {
                content.style.display = "block";
                arrow.style.transform = "rotate(180deg)";
                fetchWalletLedger();
            } else {
                content.style.display = "none";
                arrow.style.transform = "rotate(0deg)";
            }
        }

        function formatHours(hours) {
            if (hours == null || isNaN(hours)) return '—';
            if (hours < 1) return `${Math.round(hours * 60)}m`;
            if (hours < 48) return `${hours.toFixed(1)}h`;
            return `${(hours / 24).toFixed(1)}d`;
        }

        async function fetchAccountingSummary() {
            const mode = document.getElementById("historyModeFilter")?.value || "paper";
            try {
                const r = await fetch(`/api/poly-yield/accounting?mode=${encodeURIComponent(mode)}`);
                const data = await r.json();
                const t = data.totals || {};

                document.getElementById("acctTradeCount").innerText = t.trade_count || 0;
                document.getElementById("acctOpenClosedSub").innerText =
                    `${t.open_count || 0} open / ${t.won_count || 0} won / ${t.lost_count || 0} lost`;
                document.getElementById("acctWinRate").innerText = t.win_rate_pct != null ? `${t.win_rate_pct}%` : "—";
                document.getElementById("acctVolume").innerText = `$${(t.total_volume_usdc || 0).toFixed(2)}`;

                const pnlEl = document.getElementById("acctRealizedPnl");
                const pnl = t.total_realized_pnl || 0;
                pnlEl.innerText = `${pnl >= 0 ? '+' : ''}$${pnl.toFixed(2)}`;
                pnlEl.style.color = pnl >= 0 ? "var(--cyber-emerald)" : "var(--hot-pink)";

                document.getElementById("acctGasPaid").innerText = `$${(t.total_gas_usdc || 0).toFixed(3)}`;
                document.getElementById("acctAvgHold").innerText = formatHours(t.avg_hold_hours);

                // Per-strategy breakdown
                const strategyBody = document.getElementById("acctStrategyBody");
                const rows = data.by_strategy || [];
                if (rows.length === 0) {
                    strategyBody.innerHTML = `<tr><td colspan="8" style="text-align:center; color:var(--text-secondary);">No trades yet for ${escapeHtml(mode || 'this mode')}.</td></tr>`;
                } else {
                    strategyBody.innerHTML = rows.map(s => {
                        const sPnl = s.realized_pnl || 0;
                        return `
                        <tr>
                            <td><span style="font-weight:600;">${formatStrategyName(s.strategy)}</span></td>
                            <td>${s.trade_count || 0}</td>
                            <td>${s.open_count || 0}</td>
                            <td style="color:var(--cyber-emerald);">${s.won_count || 0}</td>
                            <td style="color:var(--hot-pink);">${s.lost_count || 0}</td>
                            <td>${s.win_rate_pct != null ? s.win_rate_pct + '%' : '—'}</td>
                            <td>$${(s.volume_usdc || 0).toFixed(2)}</td>
                            <td style="color:${sPnl >= 0 ? 'var(--cyber-emerald)' : 'var(--hot-pink)'}; font-weight:600;">${sPnl >= 0 ? '+' : ''}$${sPnl.toFixed(2)}</td>
                        </tr>`;
                    }).join("");
                }

                // Ledger conservation banner (paper mode only)
                const banner = document.getElementById("acctLedgerHealthBanner");
                if (data.ledger_health) {
                    const h = data.ledger_health;
                    banner.style.display = "block";
                    if (h.valid) {
                        banner.style.background = "rgba(5, 255, 201, 0.08)";
                        banner.style.border = "1px solid rgba(5, 255, 201, 0.25)";
                        banner.style.color = "var(--cyber-emerald)";
                        banner.innerText = `✅ Wallet ledger audit: ${h.status.toUpperCase()} — actual $${h.actual_balance.toFixed(2)} matches expected $${h.expected_balance.toFixed(2)} across ${h.ledger_entries} entries.`;
                    } else {
                        banner.style.background = "rgba(255, 0, 127, 0.1)";
                        banner.style.border = "1px solid rgba(255, 0, 127, 0.3)";
                        banner.style.color = "var(--hot-pink)";
                        banner.innerText = `🚨 Wallet ledger DRIFT DETECTED: actual $${h.actual_balance.toFixed(2)} vs expected $${h.expected_balance.toFixed(2)} (drift $${h.drift.toFixed(2)}). Check /api/poly-yield/wallet-health.`;
                    }
                } else {
                    banner.style.display = "none";
                }
            } catch (e) {
                console.error(e);
            }
        }

        async function fetchTradeHistory() {
            const mode = document.getElementById("historyModeFilter")?.value || "";
            const status = document.getElementById("historyStatusFilter")?.value || "";
            const executedBy = document.getElementById("historyExecFilter")?.value || "";
            const params = new URLSearchParams({ limit: "500" });
            if (mode) params.set("mode", mode);
            if (status) params.set("status", status);
            if (executedBy) params.set("executed_by", executedBy);

            try {
                const r = await fetch(`/api/poly-yield/history?${params.toString()}`);
                rawTradeHistory = await r.json();
                renderTradeHistoryTable();
            } catch (e) {
                console.error(e);
            }
        }

        function renderTradeHistoryTable() {
            const tbody = document.getElementById("tradeHistoryBody");
            const search = (document.getElementById("historySearchFilter")?.value || "").toLowerCase();

            const rows = search
                ? rawTradeHistory.filter(p => (p.market_title || "").toLowerCase().includes(search))
                : rawTradeHistory;

            if (rows.length === 0) {
                tbody.innerHTML = `<tr><td colspan="13" style="text-align:center; color:var(--text-secondary);">No trades match these filters.</td></tr>`;
                return;
            }

            tbody.innerHTML = rows.map(p => {
                const pnl = p.realized_pnl;
                const pnlHtml = pnl != null
                    ? `<span style="color:${pnl >= 0 ? 'var(--cyber-emerald)' : 'var(--hot-pink)'}; font-weight:600;">${pnl >= 0 ? '+' : ''}$${pnl.toFixed(2)}</span>`
                    : `<span style="color:var(--text-secondary);">—</span>`;
                const apyDeltaHtml = p.apy_delta != null
                    ? `<span style="color:${p.apy_delta >= 0 ? 'var(--cyber-emerald)' : 'var(--hot-pink)'};">${p.apy_delta >= 0 ? '+' : ''}${p.apy_delta.toFixed(1)}%</span>`
                    : `<span style="color:var(--text-secondary);">—</span>`;
                const exitPriceText = p.exit_price != null ? `$${p.exit_price.toFixed(4)}` : '—';
                return `
                    <tr>
                        <td><span class="badge ${getRiskBadge(p.risk_level)}">${formatStrategyName(p.strategy)}</span></td>
                        <td style="max-width:220px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;" title="${escapeHtml(p.market_title || '')}">${escapeHtml(p.market_title || '')}</td>
                        <td>${escapeHtml(p.outcome || '')}</td>
                        <td><span style="text-transform:uppercase; font-size:11px; font-weight:600; color:${p.mode === 'live' ? 'var(--hot-pink)' : 'var(--text-secondary)'};">${p.mode || 'paper'}</span></td>
                        <td>${formatExecutedByBadge(p.executed_by)}</td>
                        <td>$${(p.cost_usdc || 0).toFixed(2)}</td>
                        <td>$${(p.entry_price || 0).toFixed(4)}</td>
                        <td>${exitPriceText}</td>
                        <td style="font-size:11px; white-space:nowrap;">${formatDateTime(p.entry_at)}</td>
                        <td style="font-size:11px; white-space:nowrap;">${formatDateTime(p.settled_at)}</td>
                        <td>${pnlHtml}</td>
                        <td>${apyDeltaHtml}</td>
                        <td><span style="color: ${p.status === 'open' ? 'var(--alert-orange)' : p.status === 'won' ? 'var(--cyber-emerald)' : 'var(--hot-pink)'}; font-weight:600; text-transform:capitalize;">${p.status}</span></td>
                    </tr>
                `;
            }).join("");
        }

        async function fetchWalletLedger() {
            const tbody = document.getElementById("walletLedgerBody");
            try {
                const r = await fetch("/api/poly-yield/ledger?mode=paper&limit=200");
                const rows = await r.json();
                if (rows.length === 0) {
                    tbody.innerHTML = `<tr><td colspan="6" style="text-align:center; color:var(--text-secondary);">No ledger entries yet.</td></tr>`;
                    return;
                }
                tbody.innerHTML = rows.map(e => {
                    const amt = e.amount || 0;
                    return `
                    <tr>
                        <td style="font-size:11px; white-space:nowrap;">${formatDateTime(e.created_at)}</td>
                        <td style="font-size:11px;">${escapeHtml(e.tx_type || '')}</td>
                        <td style="color:${amt >= 0 ? 'var(--cyber-emerald)' : 'var(--hot-pink)'}; font-weight:600;">${amt >= 0 ? '+' : ''}$${amt.toFixed(2)}</td>
                        <td>$${(e.balance_before || 0).toFixed(2)}</td>
                        <td>$${(e.balance_after || 0).toFixed(2)}</td>
                        <td style="font-size:11px; max-width:280px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;" title="${escapeHtml(e.description || '')}">${escapeHtml(e.description || '')}</td>
                    </tr>`;
                }).join("");
            } catch (e) {
                console.error(e);
            }
        }

        // ---------------------------------------------------------------------
        // Dutching Bot Arena & Multi-LLM Evaluation JS Logic
        // ---------------------------------------------------------------------
        function switchMainTab(tabName) {
            document.querySelectorAll(".tab-content").forEach(el => el.style.display = "none");
            document.querySelectorAll(".nav-tab").forEach(el => el.classList.remove("active"));

            if (tabName === "dashboard") {
                document.getElementById("dashboardSection").style.display = "block";
                document.getElementById("tabBtnDashboard").classList.add("active");
            } else if (tabName === "dutching") {
                document.getElementById("dutchingArenaSection").style.display = "block";
                document.getElementById("tabBtnDutching").classList.add("active");
                fetchDutchingArena();
                fetchDutchingOpps();
            } else if (tabName === "keyvault") {
                document.getElementById("keyVaultSection").style.display = "block";
                document.getElementById("tabBtnKeyVault").classList.add("active");
            }
        }

        async function fetchDutchingArena() {
            try {
                const r = await fetch("/api/dutching/arena");
                if (!r.ok) return;
                const instances = await r.json();
                if (!Array.isArray(instances)) return;

                let totalAlloc = 0;
                let totalPnl = 0;
                let totalWins = 0;
                let totalLosses = 0;

                instances.forEach(inst => {
                    const p = inst.provider.toLowerCase();
                    totalAlloc += inst.allocated_budget_usdc || 0;
                    totalPnl += inst.total_pnl || 0;
                    totalWins += inst.win_count || 0;
                    totalLosses += inst.loss_count || 0;

                    const allocEl = document.getElementById(`alloc_${p}`);
                    if (allocEl && document.activeElement !== allocEl) {
                        allocEl.value = (inst.allocated_budget_usdc || 10.0).toFixed(1);
                    }

                    const wrEl = document.getElementById(`wr_${p}`);
                    if (wrEl) {
                        const wr = (inst.win_count + inst.loss_count) > 0
                            ? ((inst.win_count / (inst.win_count + inst.loss_count)) * 100).toFixed(1) + "%"
                            : "—";
                        wrEl.innerText = wr;
                    }

                    const pnlEl = document.getElementById(`pnl_${p}`);
                    if (pnlEl) {
                        const pnl = inst.total_pnl || 0;
                        pnlEl.innerText = `${pnl >= 0 ? '+' : ''}$${pnl.toFixed(2)}`;
                        pnlEl.style.color = pnl >= 0 ? "var(--cyber-emerald)" : "var(--hot-pink)";
                    }
                });

                document.getElementById("dutchingTotalAllocation").innerText = `$${totalAlloc.toFixed(2)} USDC`;
                const combinedTotal = totalWins + totalLosses;
                document.getElementById("dutchingArenaWinRate").innerText = combinedTotal > 0
                    ? `${((totalWins / combinedTotal) * 100).toFixed(1)}%`
                    : "—";

                const netPnlEl = document.getElementById("dutchingArenaNetPnl");
                netPnlEl.innerText = `${totalPnl >= 0 ? '+' : ''}$${totalPnl.toFixed(2)}`;
                netPnlEl.style.color = totalPnl >= 0 ? "var(--cyber-emerald)" : "var(--hot-pink)";
            } catch (e) {
                console.error("fetchDutchingArena failed:", e);
            }
        }

        async function saveModelAllocation(provider) {
            const valEl = document.getElementById(`alloc_${provider}`);
            const budget = parseFloat(valEl?.value || 10.0);
            try {
                const r = await fetch("/api/dutching/arena/allocate", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ provider: provider, allocated_budget_usdc: budget })
                });
                const res = await r.json();
                alert(`✅ Allocated $${res.allocated_budget_usdc} USDC to ${provider.toUpperCase()} model instance.`);
                fetchDutchingArena();
            } catch (e) {
                alert(`❌ Allocation failed: ${e}`);
            }
        }

        let cachedDutchingOpps = [];

        async function fetchDutchingOpps() {
            const listEl = document.getElementById("dutchingOppList");
            try {
                const r = await fetch("/api/poly-yield/opportunities");
                const allOpps = await r.json();
                // Filter multi-outcome or dutching opps
                cachedDutchingOpps = allOpps.filter(o => o.market_type === "Multi-outcome" || o.strategy === "s20_dutching" || o.strategy === "s3_buy_all");

                if (cachedDutchingOpps.length === 0) {
                    listEl.innerHTML = `<div style="text-align:center; color:var(--text-secondary); padding:40px 0;">No active multi-outcome Dutching opportunities right now. Engine is scanning CLOB...</div>`;
                    return;
                }

                listEl.innerHTML = cachedDutchingOpps.map((opp, idx) => {
                    const legs = opp.legs || [];
                    const topCandidates = opp.top_candidates || legs.map(l => l.outcome);
                    const pSum = opp.p_sum || legs.reduce((acc, l) => acc + (l.price || 0), 0);
                    return `
                        <div class="glass-panel" style="padding: 16px; border: 1px solid rgba(255,255,255,0.08);">
                            <div style="display:flex; justify-content:space-between; align-items:flex-start; margin-bottom: 8px;">
                                <span style="font-weight:700; font-family:'Space Grotesk', sans-serif; font-size:14px;">${escapeHtml(opp.market_title)}</span>
                                <span class="badge badge-low">+${(opp.expected_roi_pct || opp.profit_pct || 0).toFixed(1)}% ROI</span>
                            </div>
                            <div style="font-size:12px; color:var(--text-secondary); margin-bottom:12px;">
                                Top Candidates: <strong style="color:#fff;">${escapeHtml(topCandidates.join(", "))}</strong>
                            </div>
                            <div style="display:flex; justify-content:space-between; font-size:12px; background:rgba(255,255,255,0.02); padding:8px 12px; border-radius:8px; margin-bottom:12px;">
                                <span>Set Cost: <strong>$${(pSum || 0).toFixed(3)}</strong></span>
                                <span>Theoretical Profit: <strong style="color:var(--cyber-emerald);">$${(opp.net_profit_if_hit || opp.max_profit_usdc || 0).toFixed(2)}</strong></span>
                            </div>
                            <div style="display:flex; gap:8px;">
                                <button class="btn btn-emerald" style="padding:6px 12px; font-size:11px; flex:1;" onclick="triggerSideBySideEval(${idx})">🤖 Run Multi-LLM Evaluation</button>
                                <button class="btn" style="padding:6px 12px; font-size:11px;" onclick="executeQuickDutch(${idx})">⚡ Quick Trade</button>
                            </div>
                        </div>
                    `;
                }).join("");
            } catch (e) {
                console.error(e);
            }
        }

        async function triggerSideBySideEval(oppIdx) {
            const opp = cachedDutchingOpps[oppIdx];
            if (!opp) return;

            const displayEl = document.getElementById("evalDisplayContainer");
            const badgeEl = document.getElementById("evalStatusBadge");
            badgeEl.innerText = "Evaluating LLMs...";
            badgeEl.className = "badge badge-med";

            displayEl.innerHTML = `
                <div style="text-align:center; color:var(--cyber-emerald); padding:60px 0;">
                    <div style="font-size:16px; font-weight:700; margin-bottom:8px;">Running Parallel Multi-LLM Inference...</div>
                    <div style="font-size:12px; color:var(--text-secondary);">Querying OpenAI, Anthropic, Kimi, and DeepSeek for probability vectors &amp; tail risk...</div>
                </div>
            `;

            const candidatesPayload = (opp.legs || []).map(l => ({ name: l.outcome, price: l.price || l.fill_price || 0.1 }));
            const topSetNames = opp.top_candidates || candidatesPayload.slice(0, 3).map(c => c.name);

            try {
                const r = await fetch("/api/dutching/evaluate", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        market_question: opp.market_title,
                        market_description: opp.notes || "",
                        candidates: candidatesPayload.length > 0 ? candidatesPayload : [{ name: "Fav 1", price: 0.5 }, { name: "Fav 2", price: 0.3 }, { name: "Longshot 1", price: 0.1 }],
                        top_set_names: topSetNames,
                        providers: ["openai", "anthropic", "kimi", "deepseek"]
                    })
                });

                const data = await r.json();
                badgeEl.innerText = "Evaluation Complete";
                badgeEl.className = "badge badge-low";

                const evals = data.evaluations || {};
                displayEl.innerHTML = `
                    <div style="font-weight:700; font-family:'Space Grotesk', sans-serif; font-size:14px; margin-bottom:16px; color:#fff;">
                        ${escapeHtml(data.market_question)}
                    </div>
                    <div style="display:grid; grid-template-columns:1fr 1fr; gap:16px;">
                        ${Object.keys(evals).map(prov => {
                            const ev = evals[prov];
                            const isFallback = ev.status === "fallback";
                            const pTop = (ev.p_model_top_set * 100).toFixed(1);
                            const pTail = (ev.p_tail_risk * 100).toFixed(1);
                            const conf = (ev.confidence * 100).toFixed(0);
                            return `
                                <div class="glass-panel" style="padding:14px; background:rgba(255,255,255,0.02); border:1px solid ${isFallback ? 'rgba(255,159,28,0.3)' : 'rgba(5,255,201,0.2)'};">
                                    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
                                        <span style="font-weight:700; font-size:13px; text-transform:uppercase;">${prov}</span>
                                        <span class="badge ${isFallback ? 'badge-med' : 'badge-low'}">${isFallback ? 'Fallback' : 'Inferred'}</span>
                                    </div>
                                    <div style="display:flex; justify-content:space-between; font-size:12px; margin-bottom:4px;">
                                        <span style="color:var(--text-secondary);">Top-Set Prob:</span>
                                        <strong style="color:var(--cyber-emerald);">${pTop}%</strong>
                                    </div>
                                    <div style="display:flex; justify-content:space-between; font-size:12px; margin-bottom:4px;">
                                        <span style="color:var(--text-secondary);">Tail Risk (P_tail):</span>
                                        <strong style="color:${ev.p_tail_risk > 0.10 ? 'var(--hot-pink)' : 'var(--alert-orange)'};">${pTail}%</strong>
                                    </div>
                                    <div style="display:flex; justify-content:space-between; font-size:12px; margin-bottom:8px;">
                                        <span style="color:var(--text-secondary);">Confidence:</span>
                                        <span>${conf}%</span>
                                    </div>
                                    <div style="font-size:11px; color:var(--text-secondary); background:rgba(0,0,0,0.2); padding:6px 8px; border-radius:6px; font-style:italic;">
                                        "${escapeHtml(ev.rationale || '')}"
                                    </div>
                                </div>
                            `;
                        }).join("")}
                    </div>
                `;
            } catch (e) {
                badgeEl.innerText = "Error";
                badgeEl.className = "badge badge-high";
                displayEl.innerHTML = `<div style="color:var(--hot-pink); padding:20px 0;">Evaluation error: ${e}</div>`;
            }
        }

        function loadPresetMarket(presetId) {
            const presets = {
                1: {
                    question: "Who will win the 2028 US Presidential Election?",
                    candidates: "Trump, Harris, Vance, Newsom",
                    prices: "0.48, 0.35, 0.08, 0.04"
                },
                2: {
                    question: "Who will be nominated as the Next Federal Reserve Chair in 2026?",
                    candidates: "Kevin Warsh, Christopher Waller, Judy Shelton, Lael Brainard",
                    prices: "0.40, 0.25, 0.15, 0.05"
                },
                3: {
                    question: "Which movie will win Best Picture at the 2027 Academy Awards?",
                    candidates: "Dune Part 3, Avatar 3, Oppenheimer 2, Dark Horse Indie",
                    prices: "0.50, 0.25, 0.10, 0.05"
                }
            };
            const p = presets[presetId];
            if (!p) return;

            document.getElementById("customMarketQuestion").value = p.question;
            document.getElementById("customCandidates").value = p.candidates;
            document.getElementById("customPrices").value = p.prices;
            runCustomMarketEval();
        }

        async function runCustomMarketEval() {
            const q = document.getElementById("customMarketQuestion")?.value || "Multi-Outcome Market";
            const cStr = document.getElementById("customCandidates")?.value || "A, B, C";
            const pStr = document.getElementById("customPrices")?.value || "0.5, 0.3, 0.1";

            const cNames = cStr.split(",").map(s => s.trim()).filter(Boolean);
            const pVals = pStr.split(",").map(s => parseFloat(s.trim()) || 0.1);

            const candidatesPayload = cNames.map((name, i) => ({
                name: name,
                price: pVals[i] != null ? pVals[i] : 0.1
            }));

            const topSetNames = cNames.slice(0, 3);

            const displayEl = document.getElementById("evalDisplayContainer");
            const badgeEl = document.getElementById("evalStatusBadge");
            badgeEl.innerText = "Evaluating LLMs...";
            badgeEl.className = "badge badge-med";

            displayEl.innerHTML = `
                <div style="text-align:center; color:var(--cyber-emerald); padding:40px 0;">
                    <div style="font-size:16px; font-weight:700; margin-bottom:8px;">Running Parallel Multi-LLM Inference...</div>
                    <div style="font-size:12px; color:var(--text-secondary);">Querying OpenAI, Anthropic, Kimi, and DeepSeek for probability vectors &amp; tail risk...</div>
                </div>
            `;

            try {
                const r = await fetch("/api/dutching/evaluate", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        market_question: q,
                        market_description: "Custom user evaluation request",
                        candidates: candidatesPayload,
                        top_set_names: topSetNames,
                        providers: ["openai", "anthropic", "kimi", "deepseek"]
                    })
                });

                const data = await r.json();
                badgeEl.innerText = "Evaluation Complete";
                badgeEl.className = "badge badge-low";

                const evals = data.evaluations || {};
                displayEl.innerHTML = `
                    <div style="font-weight:700; font-family:'Space Grotesk', sans-serif; font-size:14px; margin-bottom:16px; color:#fff;">
                        ${escapeHtml(data.market_question)}
                    </div>
                    <div style="display:grid; grid-template-columns:1fr 1fr; gap:16px;">
                        ${Object.keys(evals).map(prov => {
                            const ev = evals[prov];
                            const isFallback = ev.status === "fallback";
                            const pTop = (ev.p_model_top_set * 100).toFixed(1);
                            const pTail = (ev.p_tail_risk * 100).toFixed(1);
                            const conf = (ev.confidence * 100).toFixed(0);
                            return `
                                <div class="glass-panel" style="padding:14px; background:rgba(255,255,255,0.02); border:1px solid ${isFallback ? 'rgba(255,159,28,0.3)' : 'rgba(5,255,201,0.2)'};">
                                    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
                                        <span style="font-weight:700; font-size:13px; text-transform:uppercase;">${prov}</span>
                                        <span class="badge ${isFallback ? 'badge-med' : 'badge-low'}">${isFallback ? 'Fallback' : 'Inferred'}</span>
                                    </div>
                                    <div style="display:flex; justify-content:space-between; font-size:12px; margin-bottom:4px;">
                                        <span style="color:var(--text-secondary);">Top-Set Prob:</span>
                                        <strong style="color:var(--cyber-emerald);">${pTop}%</strong>
                                    </div>
                                    <div style="display:flex; justify-content:space-between; font-size:12px; margin-bottom:4px;">
                                        <span style="color:var(--text-secondary);">Tail Risk (P_tail):</span>
                                        <strong style="color:${ev.p_tail_risk > 0.10 ? 'var(--hot-pink)' : 'var(--alert-orange)'};">${pTail}%</strong>
                                    </div>
                                    <div style="display:flex; justify-content:space-between; font-size:12px; margin-bottom:8px;">
                                        <span style="color:var(--text-secondary);">Confidence:</span>
                                        <span>${conf}%</span>
                                    </div>
                                    <div style="font-size:11px; color:var(--text-secondary); background:rgba(0,0,0,0.2); padding:6px 8px; border-radius:6px; font-style:italic;">
                                        "${escapeHtml(ev.rationale || '')}"
                                    </div>
                                </div>
                            `;
                        }).join("")}
                    </div>
                `;
            } catch (e) {
                badgeEl.innerText = "Error";
                badgeEl.className = "badge badge-high";
                displayEl.innerHTML = `<div style="color:var(--hot-pink); padding:20px 0;">Evaluation error: ${e}</div>`;
            }
        }

        async function submitVaultKey(serviceKey, inputId) {
            const val = document.getElementById(inputId)?.value;
            if (!val) {
                alert("Please enter a key value first.");
                return;
            }
            try {
                const r = await fetch("/api/dutching/keys", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ service: serviceKey, value: val })
                });
                const res = await r.json();
                alert(`✅ Encrypted ${serviceKey.toUpperCase()} key saved successfully!`);
                document.getElementById(inputId).value = "";
            } catch (e) {
                alert(`❌ Failed to save key: ${e}`);
            }
        }

        async function executeQuickDutch(oppIdx) {
            const opp = cachedDutchingOpps[oppIdx];
            if (!opp) return;

            try {
                const r = await fetch("/api/dutching/execute", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        instance_id: "inst_manual_1",
                        market_id: opp.market_id || "manual_mkt",
                        market_title: opp.market_title || "Dutch Trade",
                        legs: opp.legs || [],
                        stake_usdc: opp.target_total_cost || 10.0
                    })
                });
                const res = await r.json();
                alert(`⚡ Executed Dutching trade! Trade ID: ${res.trade_id}. New Balance: $${res.new_balance.toFixed(2)}`);
                fetchWalletBalance();
            } catch (e) {
                alert(`Execution error: ${e}`);
            }
        }

        // Init
        connectWS();
        fetchWalletBalance();
        fetchOpportunities();
        fetchPositions();
        fetchStats();

        // Refresh data periodically
        setInterval(fetchWalletBalance, 10000);
        setInterval(fetchOpportunities, 30000);
        setInterval(() => { fetchPositions(); fetchStats(); }, 60000);

        // Refresh orderbook only when visible
        setInterval(() => {
            const panel = document.getElementById('orderbookPanel');
            if (selectedTokenId && panel && panel.style.display !== 'none') {
                loadOrderBook(selectedTokenId, selectedMarketTitle);
            }
        }, 5000);
    