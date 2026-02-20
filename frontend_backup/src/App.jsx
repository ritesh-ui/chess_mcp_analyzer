import React from 'react';
import { Chessboard } from 'react-chessboard';

const App = () => {
  return (
    <div style={{
      display: 'flex', justifyContent: 'center', alignItems: 'center', minHeight: '100vh',
      backgroundColor: '#020617'
    }}>
      <div style={{ width: '480px', padding: '20px', backgroundColor: '#0f172a', borderRadius: '40px' }}>
        {/* THIS IS THE CLEAN ROOM TEST. ONLY ONE PAWN AT E4. */}
        <Chessboard
          id="CleanBoard"
          position={{ e4: 'wP' }}
          boardWidth={440}
        />
        <div style={{ color: 'white', textAlign: 'center', marginTop: '20px', fontFamily: 'monospace' }}>
          CLEAN ROOM TEST: SHOULD BE ONLY E4 PAWN
        </div>
      </div>
    </div>
  );
};

export default App;
