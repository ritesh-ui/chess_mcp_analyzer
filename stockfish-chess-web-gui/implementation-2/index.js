document.addEventListener("DOMContentLoaded", function () {
    let game = new Chess();
    let useDepth = true;
    let currentMode = 'Player vs Engine';
    let board;
    let playerColor = 'white'; // Higher scope for Coach sync
    const MCP_SERVER = 'http://localhost:8000';
    const WS_URL = 'ws://localhost:8000/ws';
    let coachSocket = null;
    let currentChallenge = null; // Track active Socratic challenge


    // --- Coach WebSocket (server -> GUI push) ---
    function connectCoachSocket() {
        coachSocket = new WebSocket(WS_URL);
        coachSocket.onopen = () => {
            document.getElementById('coach-status').textContent = '● LIVE';
            document.getElementById('coach-status').style.color = '#198754';
        };
        coachSocket.onmessage = (event) => {
            try {
                const data = JSON.parse(event.data);
                if (data.type === 'coach_tip') {
                    const panel = document.getElementById('coach-messages');
                    const time = new Date().toLocaleTimeString();
                    panel.innerHTML = `<p style="margin:0">${data.message}</p><small class="text-muted">Updated: ${time}</small>`;

                    // 1. Clear previous highlights
                    removeHighlights();

                    // 2. Apply New Heatmap Highlights
                    if (data.hot_squares) {
                        data.hot_squares.forEach(hs => {
                            const squareEl = document.querySelector(`.square-${hs.square}`);
                            if (squareEl) {
                                squareEl.classList.add(hs.type === 'gold' ? 'glow-gold' : 'glow-red');
                            }
                        });
                    }

                    // 3. Set Active Challenge
                    currentChallenge = data.challenge;

                    // Auto-expand the AI Coach accordion
                    const coachCollapse = document.getElementById('collapseCoach');
                    if (coachCollapse && !coachCollapse.classList.contains('show')) {
                        new bootstrap.Collapse(coachCollapse, { toggle: true });
                    }
                }
            } catch (e) { console.error('Coach WS error:', e); }
        };
        coachSocket.onclose = () => {
            document.getElementById('coach-status').textContent = '○ OFFLINE';
            document.getElementById('coach-status').style.color = '#6c757d';
            setTimeout(connectCoachSocket, 3000);
        };
    }

    function removeHighlights() {
        document.querySelectorAll('.glow-gold, .glow-red').forEach(el => {
            el.classList.remove('glow-gold', 'glow-red');
        });
    }

    // --- Report move to MCP server ---
    function reportToCoach(fen, pgn, lastMove, turn) {
        const payload = {
            fen,
            pgn,
            last_move: lastMove,
            turn,
            player_color: playerColor
        };
        fetch(`${MCP_SERVER}/game/sync`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        }).catch(err => console.warn('MCP sync failed (server offline?):', err));
    }

    function engineGame(options) {
        options = options || {};
        let engine = typeof STOCKFISH === "function" ? STOCKFISH() : new Worker(options.stockfishjs || './engine/stockfish-nnue-16-single.js');
        let evaler = typeof STOCKFISH === "function" ? STOCKFISH() : new Worker(options.stockfishjs || './engine/stockfish-nnue-16-single.js');
        let engineStatus = {};
        let displayScore = true;
        let isEngineRunning = false;
        let evaluation_el = document.getElementById("evaluation");
        let gameScoreEl = document.getElementById("game-score");
        let engineAnalysisEl = document.getElementById("engineAnalysis");
        let announced_game_over;
        let playerColorOriginal = playerColor;

        let onDragStart = function (source, piece, position, orientation) {
            if (currentMode === 'Player vs Player') {
                return !game.game_over();
            }
            let re = playerColor == 'white' ? /^b/ : /^w/;
            if (game.game_over() || piece.search(re) !== -1) {
                return false;
            }
        };

        let onClickPiece = function (source, target) {
            let move = game.move({
                from: source,
                to: target,
                promotion: document.getElementById("promote").value
            });
            if (move === null) return 'snapback';
            prepareMove();
        };

        setInterval(function () {
            if (announced_game_over) {
                return;
            }
            if (game.game_over()) {
                announced_game_over = true;
                $('#game-score').text("Game Over");
            }
        }, 1000);

        function uciCmd(cmd, which) {
            console.log("UCI: " + cmd);
            (which || engine).postMessage(cmd);
        }
        uciCmd('uci');

        function displayStatus() {
            let status = 'Stockfish 16 NNUE => ';
            if (engineStatus.search) {
                status += engineStatus.search + ' | ';
                if (engineStatus.score && displayScore) {
                    status += (engineStatus.score.substr(0, 4) === "Mate" ? " " : ' Score: ') + engineStatus.score;
                }
            }
            $('#game-score').html(status);
            updateScoreBar(engineStatus.score || 0);
        }

        function updateScoreBar(score) {
            let scoreBar = document.getElementById('score-bar');
            let maxScore = 10;
            let scorePercentage = (score / maxScore) * 50 + 50;

            scorePercentage = Math.max(0, Math.min(100, scorePercentage));

            scoreBar.style.height = scorePercentage + '%';
        }

        function get_moves() {
            let moves = '';
            let history = game.history({ verbose: true });

            for (let i = 0; i < history.length; ++i) {
                let move = history[i];
                moves += ' ' + move.from + move.to + (move.promotion ? move.promotion : '');
            }

            return moves;
        }

        function prepareMove() {
            $('#pgn').text(game.pgn());
            document.getElementById("pgnInput").value = game.pgn();
            document.getElementById("fenInput").value = game.fen();
            board.position(game.fen());
            removeHighlights();

            // --- Report to MCP Coach ---
            const history = game.history({ verbose: true });
            const lastMove = history.length > 0
                ? history[history.length - 1].from + history[history.length - 1].to
                : null;
            const turn = game.turn() === 'w' ? 'white' : 'black';
            reportToCoach(game.fen(), game.pgn(), lastMove, turn);

            if (currentMode === 'Player vs Engine') {
                let turn = game.turn() == 'w' ? 'white' : 'black';
                if (!game.game_over() && turn != playerColor) {
                    uciCmd('position startpos moves' + get_moves());
                    uciCmd('position startpos moves' + get_moves(), evaler);
                    evaluation_el.textContent = "";
                    engineAnalysisEl.textContent = "";
                    uciCmd("eval", evaler);

                    if (useDepth) {
                        let depth = parseInt(document.getElementById("depthLevel").value, 10);
                        depth = Math.max(1, Math.min(30, depth));
                        uciCmd("go depth " + depth);
                    } else {
                        let time = parseInt(document.getElementById("thinkingTime").value, 10) * 1000;
                        time = Math.max(1000, Math.min(30000, time));
                        uciCmd("go movetime " + time);
                    }
                    isEngineRunning = true;
                }
            }
        }

        evaler.onmessage = function (event) {
            let line;
            if (event && typeof event === "object") {
                line = event.data;
            } else {
                line = event;
            }
            console.log("evaler: " + line);
            if (line === "uciok" || line === "readyok" || line.substr(0, 11) === "option name") {
                return;
            }
            if (evaluation_el.textContent) {
                evaluation_el.textContent += "\n";
            }
            evaluation_el.textContent += line;
        };

        engine.onmessage = function (event) {
            let line;
            if (event && typeof event === "object") {
                line = event.data;
            } else {
                line = event;
            }
            console.log("Reply: " + line);
            if (line == 'uciok') {
                engineStatus.engineLoaded = true;
            } else if (line == 'readyok') {
                engineStatus.engineReady = true;
                displayStatus();
            } else {
                let match = line.match(/^bestmove ([a-h][1-8])([a-h][1-8])([qrbn])?/);
                if (match) {
                    isEngineRunning = false;
                    game.move({ from: match[1], to: match[2], promotion: match[3] });
                    prepareMove();
                    uciCmd("eval", evaler);
                    evaluation_el.textContent = "";
                } else if (match = line.match(/^info .*\bdepth (\d+) .*\bnps (\d+)/)) {
                    engineStatus.search = 'Depth: ' + match[1] + ' Nps: ' + match[2];
                }
                if (match = line.match(/^info .*\bscore (\w+) (-?\d+)/)) {
                    let score = parseInt(match[2]) * (game.turn() == 'w' ? 1 : -1);
                    if (match[1] == 'cp') {
                        engineStatus.score = (score / 100.0).toFixed(2);
                    } else if (match[1] == 'mate') {
                        engineStatus.score = 'Mate in ' + Math.abs(score);
                    }
                    if (match = line.match(/\b(upper|lower)bound\b/)) {
                        engineStatus.score = ((match[1] == 'upper') == (game.turn() == 'w') ? '<= ' : '>= ') + engineStatus.score;
                    }
                }

                let analysisMatch = line.match(/^info .*\bpv ((?:[a-h][1-8][a-h][1-8][qrbn]? ?)+)/);
                if (analysisMatch) {
                    engineAnalysisEl.textContent = analysisMatch[1];
                }

                gameScoreEl.textContent = engineStatus.score;
            }
            displayStatus();
        };

        let onDrop = function (source, target) {
            let move = game.move({
                from: source,
                to: target,
                promotion: document.getElementById("promote").value
            });
            if (move === null) return 'snapback';
            prepareMove();
        };

        let onSnapEnd = function () {
            board.position(game.fen());
        };

        let onClick = function (source, target) {
            // Check for Socratic Challenge response
            if (currentChallenge && source === currentChallenge.target_square) {
                const panel = document.getElementById('coach-messages');
                panel.innerHTML = `<div class="alert alert-success mt-2">✨ <strong>Perfect!</strong> You found the critical piece. That is exactly where the tension is focused!</div>` + panel.innerHTML;
                removeHighlights();
                currentChallenge = null;
            }

            onClickPiece(source, target);
            prepareMove();
        };

        let cfg = {
            showErrors: true,
            draggable: true,
            position: 'start',
            onDragStart: onDragStart,
            onDrop: onDrop,
            onSnapEnd: onSnapEnd,
            onClick: onClick
        };

        board = new ChessBoard('board', cfg);

        return {
            reset: function () {
                game.reset();
                removeHighlights();
                currentChallenge = null;

                uciCmd('setoption name Contempt value 0');
                this.setSkillLevel(0);
                uciCmd('setoption name King Safety value 0');
                prepareMove();
            },
            setPlayerColor: function (color) {
                playerColor = color;
                board.orientation(playerColor);
            },
            setSkillLevel: function (skill) {
                if (skill < 0) {
                    skill = 0;
                }
                if (skill > 20) {
                    skill = 20;
                }
                uciCmd('setoption name Skill Level value ' + skill);
                let max_err = Math.round((skill * -0.5) + 10);
                let err_prob = Math.round((skill * 6.35) + 1);
                uciCmd('setoption name Skill Level Maximum Error value ' + max_err);
                uciCmd('setoption name Skill Level Probability value ' + err_prob);
            },
            setDepth: function (depth) {
                uciCmd('setoption name Depth value ' + depth);
            },
            setNodes: function (nodes) {
                uciCmd('setoption name Nodes value ' + nodes);
            },
            setContempt: function (contempt) {
                uciCmd('setoption name Contempt value ' + contempt);
            },
            setAggressiveness: function (value) {
                uciCmd('setoption name Aggressiveness value ' + value);
            },
            setDisplayScore: function (flag) {
                displayScore = flag;
                displayStatus();
            },
            start: function () {
                uciCmd('ucinewgame');
                uciCmd('isready');
                engineStatus.engineReady = false;
                engineStatus.search = null;
                displayStatus();
                prepareMove();
                announced_game_over = false;

                // Clear AI Coach Memory UI
                const chatHistory = document.getElementById('coach-chat-history');
                if (chatHistory) chatHistory.innerHTML = '<div class="text-muted small text-center italic">Type a question below to start a conversation...</div>';

                const coachMsg = document.getElementById('coach-messages');
                if (coachMsg) coachMsg.innerHTML = '<div class="text-center py-3"><div class="spinner-border spinner-border-sm text-primary" role="status"></div><div class="mt-2 text-muted small">Waking up the Grandmaster...</div></div>';
            },
            undo: function () {
                game.undo();
                game.undo();
                prepareMove();
            },
            flipBoard: function () {
                board.flip();
            },
            switchBoard: function () {
                playerColorOriginal = playerColor;
                playerColor = (playerColor === 'white') ? 'black' : 'white';
                board.orientation(playerColor);
                prepareMove();
            },
            setMode: function (mode) {
                currentMode = mode;
                displayStatus();
            }
        };
    }

    let gameInstance = engineGame();

    // Connect coach WebSocket after game is initialised
    connectCoachSocket();

    function adjustScoreBarHeight() {
        const boardElement = document.getElementById('board');
        const scoreBarContainer = document.getElementById('score-bar-container');
        const boardHeight = boardElement.offsetHeight;
        scoreBarContainer.style.height = `${boardHeight}px`;
    }

    adjustScoreBarHeight();
    window.addEventListener('resize', adjustScoreBarHeight);

    document.getElementById("skillLevel").addEventListener("change", function () {
        let skillLevel_parsed = parseInt(this.value, 10);
        if (skillLevel_parsed > 20) {
            document.getElementById("skillLevel").value = 20;
        } else if (skillLevel_parsed < 0) {
            document.getElementById("skillLevel").value = 0;
        }
        gameInstance.setSkillLevel(skillLevel_parsed);
    });

    document.getElementById("depthLevel").addEventListener("change", function () {
        let depthLevel_parsed = parseInt(this.value, 10);
        if (depthLevel_parsed > 30) {
            document.getElementById("depthLevel").value = 30;
        } else if (depthLevel_parsed < 1) {
            document.getElementById("depthLevel").value = 1;
        }
        gameInstance.setDepth(depthLevel_parsed);
    });

    document.getElementById("thinkingTime").addEventListener("change", function () {
        let thinkingTime_parsed = parseInt(this.value, 10);
        if (thinkingTime_parsed > 30) {
            document.getElementById("thinkingTime").value = 30;
        } else if (thinkingTime_parsed < 1) {
            document.getElementById("thinkingTime").value = 1;
        }
    });

    document.getElementById("depthToggle").addEventListener("change", function () {
        useDepth = true;
    });

    document.getElementById("timeToggle").addEventListener("change", function () {
        useDepth = false;
    });

    document.getElementById("promote").addEventListener("change", function () {
        gameInstance.setPlayerColor(document.querySelector('input[name="color"]:checked').value);
    });

    document.getElementById("color-white").addEventListener("change", function () {
        gameInstance.setPlayerColor('white');
    });

    document.getElementById("color-black").addEventListener("change", function () {
        gameInstance.setPlayerColor('black');
    });

    document.getElementById("newGameBtn").addEventListener("click", function () {
        gameInstance.reset();
        gameInstance.start();
        removeHighlights();
        currentChallenge = null;
    });

    document.getElementById("takeBackBtn").addEventListener("click", function () {
        gameInstance.undo();
    });

    document.getElementById("flipBoardBtn").addEventListener("click", function () {
        gameInstance.flipBoard();
    });

    document.getElementById("switchBoardBtn").addEventListener("click", function () {
        gameInstance.switchBoard();
    });

    document.getElementById("resetGameBtn").addEventListener("click", function () {
        gameInstance.reset();
        gameInstance.start();
        removeHighlights();
        currentChallenge = null;
    });

    document.getElementById("gameMode").addEventListener("change", function () {
        gameInstance.setMode(this.value);
    });

    document.getElementById("copyPgnBtn").addEventListener("click", function () {
        const pgnText = game.pgn();
        document.getElementById("pgnInput").value = pgnText;
        navigator.clipboard.writeText(pgnText);
    });

    document.getElementById("copyFenBtn").addEventListener("click", function () {
        const fenText = game.fen();
        document.getElementById("fenInput").value = fenText;
        navigator.clipboard.writeText(fenText);
    });

    // --- AI Coach Chat Logic ---
    async function askCoach() {
        const input = document.getElementById('coach-chat-input');
        const history = document.getElementById('coach-chat-history');
        const sendBtn = document.getElementById('coach-chat-send');
        const question = input.value.trim();

        if (!question) return;

        // 1. Add User message to UI
        const userMsg = document.createElement('div');
        userMsg.style = "align-self: flex-end; background: #e7f3ff; padding: 8px 12px; border-radius: 12px 12px 0 12px; max-width: 85%; border: 1px solid #cce5ff;";
        userMsg.innerHTML = `<strong>You:</strong> ${question}`;
        history.appendChild(userMsg);

        // Clear input and scroll
        input.value = '';
        history.scrollTop = history.scrollHeight;

        // 2. Disable input while thinking
        input.disabled = true;
        sendBtn.disabled = true;

        const loadingMsg = document.createElement('div');
        loadingMsg.style = "align-self: flex-start; background: #f1f3f5; padding: 8px 12px; border-radius: 12px 12px 12px 0; max-width: 85%; border: 1px solid #dee2e6;";
        loadingMsg.innerHTML = `<em>The Grandmaster is thinking...</em>`;
        history.appendChild(loadingMsg);
        history.scrollTop = history.scrollHeight;

        // 3. Request to MCP Server
        try {
            const response = await fetch(`${MCP_SERVER}/coach/query`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    fen: game.fen(),
                    pgn: game.pgn(),
                    question: question,
                    player_color: playerColor
                })
            });
            const data = await response.json();

            // Remove loading
            history.removeChild(loadingMsg);

            // Add Coach response
            const coachMsg = document.createElement('div');
            coachMsg.style = "align-self: flex-start; background: #fff; padding: 8px 12px; border-radius: 12px 12px 12px 0; max-width: 85%; border: 1px solid #dee2e6; box-shadow: 0 1px 2px rgba(0,0,0,0.05);";
            coachMsg.innerHTML = `<strong>Coach:</strong> ${data.response || 'Sorry, I lost my train of thought.'}`;
            history.appendChild(coachMsg);

        } catch (err) {
            console.error('Coach Query failed:', err);
            history.removeChild(loadingMsg);
            const errMsg = document.createElement('div');
            errMsg.className = "text-danger small";
            errMsg.textContent = "Error communicating with the Grandmaster. Is the server running?";
            history.appendChild(errMsg);
        } finally {
            input.disabled = false;
            sendBtn.disabled = false;
            history.scrollTop = history.scrollHeight;
            input.focus();
        }
    }

    document.getElementById('coach-chat-send').addEventListener('click', askCoach);
    document.getElementById('coach-chat-input').addEventListener('keypress', (e) => {
        if (e.key === 'Enter') askCoach();
    });

    gameInstance.start();
});
