document.addEventListener("DOMContentLoaded", function () {
    let game = new Chess();
    let useDepth = true;
    let currentMode = 'Player vs Engine';
    let board;
    let playerColor = 'white';
    const MCP_SERVER = 'http://localhost:8000';
    const WS_URL = 'ws://localhost:8000/ws';
    let coachSocket = null;
    let currentChallenge = null;
    let drillData = null;
    let isDrillMode = false;

    // --- Audio Engine ---
    const moveSound = new Audio('https://images.chesscomfiles.com/chess-themes/sounds/_MP3_/default/move-self.mp3');
    const captureSound = new Audio('https://images.chesscomfiles.com/chess-themes/sounds/_MP3_/default/capture.mp3');

    function playMoveSound(isCapture) {
        if (isCapture) captureSound.play().catch(e => { });
        else moveSound.play().catch(e => { });
    }



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
                    panel.innerHTML = `
                        <div class="chat-bubble bubble-coach shadow-sm w-100">
                            <p style="margin:0">${data.message}</p>
                            <div class="text-end mt-1" style="font-size: 0.7rem; opacity: 0.6;">${time}</div>
                        </div>
                    `;

                    removeHighlights();

                    if (data.hot_squares) {
                        data.hot_squares.forEach(hs => {
                            const squareEl = document.querySelector(`.square-${hs.square}`);
                            if (squareEl) {
                                squareEl.classList.add(hs.type === 'gold' ? 'glow-gold' : 'glow-red');
                            }
                        });
                    }

                    // 2.5 Detection for "Brilliant" move ignite
                    if (data.message && data.message.includes('Great Move')) {
                        const targetSquare = data.hot_squares.find(s => s.type === 'gold')?.square;
                        if (targetSquare) {
                            const sqEl = document.querySelector(`.square-${targetSquare}`);
                            if (sqEl) {
                                sqEl.classList.add('brilliant-ignite');
                                setTimeout(() => sqEl.classList.remove('brilliant-ignite'), 1500);
                            }
                        }
                    }

                    currentChallenge = data.challenge;

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

    function reportToCoach(fen, pgn, lastMove, turn) {
        const payload = {
            fen,
            pgn,
            last_move: lastMove,
            turn,
            player_color: playerColor,
            analyze_cpu: document.getElementById('analyze-cpu-toggle').checked
        };
        fetch(`${MCP_SERVER}/game/sync`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        }).catch(err => console.warn('MCP sync failed:', err));
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
        let announced_game_over = false;
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
                triggerGameReview();
            }
        }, 1000);

        async function triggerGameReview() {
            try {
                const response = await fetch(`${MCP_SERVER}/game/review`, { method: 'POST' });
                const data = await response.json();

                const reviewPanel = document.getElementById('review-accordion-item');
                const lessonsList = document.getElementById('review-lessons');

                if (reviewPanel) {
                    reviewPanel.style.display = 'block';
                    lessonsList.innerHTML = '';

                    data.lessons.forEach(lesson => {
                        const div = document.createElement('div');
                        div.className = 'mb-2 p-2 rounded';
                        div.style.background = 'rgba(16, 185, 129, 0.1)';
                        div.style.border = '1px solid rgba(16, 185, 129, 0.2)';
                        div.style.color = '#d1fae5';
                        div.style.fontSize = '0.9rem';
                        div.innerHTML = `✅ ${lesson}`;
                        lessonsList.appendChild(div);
                    });

                    if (data.blunder) {
                        drillData = data.blunder;
                        document.getElementById('blunder-drill-container').style.display = 'block';
                    } else {
                        document.getElementById('blunder-drill-container').style.display = 'none';
                    }

                    const reviewCollapse = document.getElementById('collapseReview');
                    if (reviewCollapse && !reviewCollapse.classList.contains('show')) {
                        new bootstrap.Collapse(reviewCollapse, { toggle: true });
                    }
                }
            } catch (err) { console.error('Review failed:', err); }
        }

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

                    let thinkingTime_val = parseInt(document.getElementById("thinkingTime").value, 10);
                    if (useDepth) {
                        let depth = parseInt(document.getElementById("depthLevel").value, 10);
                        uciCmd("go depth " + depth);
                    } else {
                        uciCmd("go movetime " + (thinkingTime_val * 1000));
                    }
                    isEngineRunning = true;
                }
            }
        }

        evaler.onmessage = function (event) {
            let line = event.data || event;
            if (line === "uciok" || line === "readyok" || line.substr(0, 11) === "option name") return;
            if (evaluation_el.textContent) evaluation_el.textContent += "\n";
            evaluation_el.textContent += line;
        };

        engine.onmessage = function (event) {
            let line = event.data || event;
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
                }
                let analysisMatch = line.match(/^info .*\bpv ((?:[a-h][1-8][a-h][1-8][qrbn]? ?)+)/);
                if (analysisMatch) engineAnalysisEl.textContent = analysisMatch[1];
                gameScoreEl.textContent = engineStatus.score;
            }
            displayStatus();
        };

        let onClick = function (source, target) {
            if (currentChallenge && source === currentChallenge.target_square) {
                const panel = document.getElementById('coach-messages');
                panel.innerHTML = `<div class="alert alert-success mt-2">✨ <strong>Perfect!</strong> You found the critical piece.</div>` + panel.innerHTML;
                removeHighlights();
                currentChallenge = null;
            }
            onClickPiece(source, target);
            prepareMove();
        };

        let onDrop = function (source, target) {
            if (isDrillMode) {
                const moveObj = { from: source, to: target, promotion: document.getElementById("promote").value };
                const moveInSan = new Chess(drillData.fen).move(moveObj)?.san;
                if (moveInSan === drillData.best_move) {
                    const panel = document.getElementById('coach-messages');
                    panel.innerHTML = `<div class="alert alert-success mt-2">✨ <strong>Lesson Learned!</strong><br>Yes! ${moveInSan} was the winning continuation.</div>`;
                    isDrillMode = false;
                    currentMode = 'Player vs Engine';
                } else {
                    const panel = document.getElementById('coach-messages');
                    panel.innerHTML = `<div class="alert alert-warning mt-2">❌ <strong>Not quite.</strong> Try again!</div>` + panel.innerHTML;
                    return 'snapback';
                }
            }

            let move = game.move({ from: source, to: target, promotion: document.getElementById("promote").value });
            if (move === null) return 'snapback';

            playMoveSound(move.captured);
            prepareMove();
        };

        let cfg = {
            draggable: true,
            position: 'start',
            onDragStart: onDragStart,
            onDrop: onDrop,
            onClick: onClick,
            onSnapEnd: function () { board.position(game.fen()); }
        };
        board = new ChessBoard('board', cfg);

        return {
            reset: function () {
                game.reset();
                removeHighlights();
                currentChallenge = null;
                document.getElementById('review-accordion-item').style.display = 'none';
                prepareMove();
            },
            setPlayerColor: function (color) {
                playerColor = color;
                board.orientation(playerColor);
            },
            setSkillLevel: function (skill) {
                uciCmd('setoption name Skill Level value ' + skill);
            },
            setDepth: function (depth) {
                uciCmd('setoption name Depth value ' + depth);
            },
            start: function () {
                uciCmd('ucinewgame');
                uciCmd('isready');
                announced_game_over = false;
                prepareMove();
            },
            undo: function () {
                game.undo(); game.undo();
                prepareMove();
            },
            flipBoard: function () { board.flip(); },
            switchBoard: function () {
                playerColor = (playerColor === 'white') ? 'black' : 'white';
                board.orientation(playerColor);
                prepareMove();
            },
            setMode: function (mode) {
                currentMode = mode;
                displayStatus();
            },
            stop: function () {
                uciCmd('stop');
                uciCmd('stop', evaler);
                isEngineRunning = false;
            }
        };
    }

    let gameInstance = engineGame();
    connectCoachSocket();

    function adjustScoreBarHeight() {
        const boardElement = document.getElementById('board');
        const scoreBarContainer = document.getElementById('score-bar-container');
        if (boardElement && scoreBarContainer) {
            scoreBarContainer.style.height = `${boardElement.offsetHeight}px`;
        }
    }
    adjustScoreBarHeight();
    window.addEventListener('resize', adjustScoreBarHeight);

    document.getElementById("newGameBtn").addEventListener("click", () => {
        gameInstance.stop();
        gameInstance.reset();
        gameInstance.start();
        clearAIChat();
        fetch(`${MCP_SERVER}/reset`, { method: 'POST' }).catch(e => console.error("Backend reset failed:", e));
    });
    document.getElementById("resetGameBtn").addEventListener("click", () => {
        gameInstance.stop();
        gameInstance.reset();
        gameInstance.start();
        clearAIChat();
        fetch(`${MCP_SERVER}/reset`, { method: 'POST' }).catch(e => console.error("Backend reset failed:", e));
    });

    function clearAIChat() {
        document.getElementById("coach-messages").innerHTML = `
            <div class="text-center py-4 opacity-50">
                <p class="small mb-0">Board reset. Play a move to begin analysis.</p>
            </div>
        `;
    }
    document.getElementById("takeBackBtn").addEventListener("click", () => gameInstance.undo());
    document.getElementById("flipBoardBtn").addEventListener("click", () => gameInstance.flipBoard());
    document.getElementById("switchBoardBtn").addEventListener("click", () => gameInstance.switchBoard());

    document.getElementById("gameMode").addEventListener("change", function () { gameInstance.setMode(this.value); });
    document.getElementById("color-white").addEventListener("change", () => gameInstance.setPlayerColor('white'));
    document.getElementById("color-black").addEventListener("change", () => gameInstance.setPlayerColor('black'));

    // --- AI Coach Chat ---
    async function askCoach() {
        const input = document.getElementById('coach-chat-input');
        const history = document.getElementById('coach-chat-history');
        const question = input.value.trim();
        if (!question) return;

        const userMsg = document.createElement('div');
        userMsg.className = "chat-bubble bubble-user shadow-sm";
        userMsg.innerHTML = `<strong>You:</strong> ${question}`;
        history.appendChild(userMsg);
        input.value = '';

        // Add loading indicator
        const loadingMsg = document.createElement('div');
        loadingMsg.className = "chat-bubble bubble-coach shadow-sm";
        loadingMsg.id = "coach-loading-bubble";
        loadingMsg.innerHTML = `<strong>Coach:</strong> <span class="spinner-border spinner-border-sm text-secondary" role="status" aria-hidden="true" style="margin-left:5px; margin-right:5px"></span> <em>Thinking...</em>`;
        history.appendChild(loadingMsg);
        history.scrollTop = history.scrollHeight;

        try {
            // Existing fetch for coach query
            const response = await fetch(`${MCP_SERVER}/coach/query`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ fen: game.fen(), pgn: game.pgn(), question: question, player_color: playerColor })
            });
            const data = await response.json();

            // Remove loading indicator
            const loader = document.getElementById("coach-loading-bubble");
            if (loader) loader.remove();

            const coachMsg = document.createElement('div');
            coachMsg.className = "chat-bubble bubble-coach shadow-sm";
            coachMsg.innerHTML = `<strong>Coach:</strong> ${data.response}`;
            history.appendChild(coachMsg);

            // New fetch to sync CPU analysis toggle
            fetch(`${MCP_SERVER}/game/sync`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    fen: game.fen(),
                    pgn: game.pgn(),
                    last_move: null, // No specific last move for a chat query
                    turn: game.turn() === 'w' ? 'white' : 'black',
                    player_color: playerColor,
                    analyze_cpu: document.getElementById('analyze-cpu-toggle').checked
                })
            }).catch(e => { console.error("Failed to sync CPU analysis toggle:", e); });

        } catch (err) { console.error(err); }
        history.scrollTop = history.scrollHeight;
    }

    document.getElementById('coach-chat-send').addEventListener('click', askCoach);
    document.getElementById('coach-chat-input').addEventListener('keypress', (e) => { if (e.key === 'Enter') askCoach(); });

    // --- Blunder Drill ---
    function startBlunderDrill() {
        if (!drillData) return;
        isDrillMode = true;
        game.load(drillData.fen);
        board.position(drillData.fen);
        const panel = document.getElementById('coach-messages');
        panel.innerHTML = `<div class="alert alert-danger">🚨 <strong>Blunder Drill Started!</strong><br>You played ${drillData.played_move}. Find the correct move!</div>`;
        currentMode = 'Player vs Player';
    }
    document.getElementById('startDrillBtn').addEventListener('click', startBlunderDrill);

    gameInstance.start();
});
