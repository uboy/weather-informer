package ru.weather.informer;

import android.util.Log;

import org.json.JSONArray;
import org.json.JSONObject;
import org.webrtc.IceCandidate;
import org.webrtc.MediaConstraints;
import org.webrtc.PeerConnection;
import org.webrtc.SdpObserver;
import org.webrtc.SessionDescription;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;

/**
 * Одна сессия WebRTC с одним зрителем (фаза 1 камер).
 * Держит PeerConnection поверх общих video/audio треков CameraService.
 * Все вложенные классы именованные (гоча d8 NPE на анонимных inner-классах).
 */
public class CamPeerSession {
    private static final String TAG = "CamPeerSession";

    /** Куда слать сообщения к конкретному зрителю. */
    public interface SignalingSink {
        boolean sendToViewer(String viewerId, JSONObject message);
    }

    public final String viewerId;
    private final CameraService service;
    private final SignalingSink sink;
    private PeerConnection pc;
    private volatile boolean answerSent = false;
    private volatile boolean closed = false;

    // ===== Pull-режим (sink == null): сервер сам прислал offer HTTP-запросом =====
    // Кандидаты не trickle-ятся через WS, а собираются локально до COMPLETE и
    // уезжают одним HTTP-ответом (non-trickle) вместе с answer.
    private final boolean audioEnabled;
    private final List<IceCandidate> localCandidates = new ArrayList<IceCandidate>();
    private final CountDownLatch answerReady = new CountDownLatch(1);
    private final CountDownLatch gatherDone = new CountDownLatch(1);
    private volatile String answerSdpText = "";
    private volatile String pullError = "";

    public CamPeerSession(String viewerId, CameraService service, SignalingSink sink) {
        this(viewerId, service, sink, true);
    }

    /** audioEnabled=false: зритель не просил звук - микрофонный трек в answer не попадает. */
    public CamPeerSession(String viewerId, CameraService service, SignalingSink sink,
                          boolean audioEnabled) {
        this.viewerId = viewerId;
        this.service = service;
        this.sink = sink;
        this.audioEnabled = audioEnabled;
    }

    public boolean isClosed() {
        return closed;
    }

    /** Обработать offer зрителя: создать PC, ответить answer. */
    public void handleOffer(JSONObject payload) {
        String type = payload.optString("type", "");
        String sdp = payload.optString("sdp", "");
        if (!"offer".equals(type) || sdp.isEmpty()) {
            Log.w(TAG, "handleOffer: bad payload from " + viewerId);
            return;
        }
        if (pc != null) {
            Log.w(TAG, "re-offer from " + viewerId + ": closing previous PC");
            closePc();
        }
        PeerConnection.RTCConfiguration config =
                new PeerConnection.RTCConfiguration(Arrays.asList(new PeerConnection.IceServer[0]));
        config.sdpSemantics = PeerConnection.SdpSemantics.UNIFIED_PLAN;
        CamPcObserver observer = new CamPcObserver(this);
        pc = service.getFactory().createPeerConnection(config, observer);
        if (pc == null) {
            service.notifySessionFailed(viewerId, "createPeerConnection failed");
            return;
        }
        if (service.getVideoTrack() != null) {
            pc.addTrack(service.getVideoTrack(), Arrays.asList("weather-cam"));
        }
        if (audioEnabled && service.getAudioTrack() != null) {
            pc.addTrack(service.getAudioTrack(), Arrays.asList("weather-cam"));
        }
        pc.setBitrate(150000, 350000, 500000);
        SessionDescription remote =
                new SessionDescription(SessionDescription.Type.OFFER, sdp);
        pc.setRemoteDescription(new CamSetRemoteObserver(this), remote);
    }

    /** Обработать ICE-кандидат от зрителя. */
    public void handleRemoteIce(JSONObject payload) {
        if (pc == null || closed) {
            return;
        }
        String sdpMid = payload.optString("sdpMid", "0");
        int mLine = payload.optInt("sdpMLineIndex", 0);
        String sdp = payload.optString("candidate", "");
        if (sdp.isEmpty()) {
            return;
        }
        pc.addIceCandidate(new IceCandidate(sdpMid, mLine, sdp));
    }

    public void close(String reason) {
        if (closed) {
            return;
        }
        closed = true;
        Log.i(TAG, "close session " + viewerId + ": " + reason);
        closePc();
    }

    private void closePc() {
        PeerConnection ref = pc;
        pc = null;
        if (ref != null) {
            try {
                ref.close();
            } catch (Exception e) {
                Log.w(TAG, "pc.close: " + e);
            }
        }
    }

    private void sendAnswer(SessionDescription sdp) {
        if (sink == null) {
            // Pull-режим: ответ заберёт HTTP-поток через waitForAnswerAndIce.
            answerSdpText = sdp.description;
            answerReady.countDown();
            return;
        }
        JSONObject msg = new JSONObject();
        jput(msg, "type", "answer");
        JSONObject payload = new JSONObject();
        jput(payload, "type", "answer");
        jput(payload, "sdp", sdp.description);
        jput(msg, "payload", payload);
        sink.sendToViewer(viewerId, msg);
    }

    private void sendLocalIce(IceCandidate candidate) {
        if (sink == null) {
            synchronized (localCandidates) {
                localCandidates.add(candidate);
            }
            return;
        }
        JSONObject msg = new JSONObject();
        jput(msg, "type", "ice");
        JSONObject payload = new JSONObject();
        jput(payload, "candidate", candidate.sdp);
        jput(payload, "sdpMid", candidate.sdpMid);
        jput(payload, "sdpMLineIndex", candidate.sdpMLineIndex);
        jput(msg, "payload", payload);
        sink.sendToViewer(viewerId, msg);
    }

    private static void jput(JSONObject o, String key, Object value) {
        try {
            o.put(key, value);
        } catch (Exception e) {
            throw new IllegalStateException("json put failed: " + key, e);
        }
    }

    // ===== Pull-режим: API для HTTP-обработчика планшета =====

    /** Ошибка старта сессии на camThread (capture/регистрация) - пустая строка = нет ошибки. */
    public String pullError() {
        return pullError;
    }

    /** Пометить сессию ошибшей: ожидающий HTTP-поток получит отказ сразу, а не по таймауту. */
    public void failPull(String reason) {
        pullError = reason == null || reason.isEmpty() ? "session failed" : reason;
        answerReady.countDown();
        gatherDone.countDown();
    }

    /**
     * Дождаться answer и сбора ICE-кандидатов (host-only, без STUN, обычно < 1 с).
     * Вызывается из HTTP-потока; мы не держим мониторы CameraService.
     * false = таймаут или ошибка старта (см. pullError()).
     */
    public boolean waitForAnswerAndIce(long timeoutMs) {
        try {
            if (!answerReady.await(timeoutMs, TimeUnit.MILLISECONDS)) {
                return false;
            }
            if (!pullError.isEmpty()) {
                return false;
            }
            // Хвост на сбор кандидатов: если gathering завис (нет сетевых интерфейсов),
            // отдаём answer с тем, что успело собраться.
            gatherDone.await(Math.max(1000L, timeoutMs / 2), TimeUnit.MILLISECONDS);
            return !pullError.isEmpty() ? false : !answerSdpText.isEmpty();
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            return false;
        }
    }

    /** Тело HTTP-ответа планшета: {type:"answer", sdp, candidates:[{candidate,sdpMid,sdpMLineIndex}]}. */
    public JSONObject buildAnswerJson() {
        JSONObject o = new JSONObject();
        jput(o, "type", "answer");
        jput(o, "sdp", answerSdpText);
        JSONArray cands = new JSONArray();
        synchronized (localCandidates) {
            for (IceCandidate c : localCandidates) {
                JSONObject c2 = new JSONObject();
                jput(c2, "candidate", c.sdp);
                jput(c2, "sdpMid", c.sdpMid);
                jput(c2, "sdpMLineIndex", c.sdpMLineIndex);
                cands.put(c2);
            }
        }
        jput(o, "candidates", cands);
        return o;
    }

    // ===== Именованные наблюдатели org.webrtc =====

    /** PeerConnection.Observer сессии. */
    private static final class CamPcObserver implements PeerConnection.Observer {
        private final CamPeerSession session;

        CamPcObserver(CamPeerSession session) {
            this.session = session;
        }

        @Override
        public void onIceCandidate(IceCandidate candidate) {
            session.sendLocalIce(candidate);
        }

        @Override
        public void onConnectionChange(PeerConnection.PeerConnectionState newState) {
            Log.i(TAG, "pc " + session.viewerId + " state: " + newState);
            if (newState == PeerConnection.PeerConnectionState.FAILED
                    || newState == PeerConnection.PeerConnectionState.CLOSED) {
                session.service.dropSession(session.viewerId, "pc " + newState);
            }
        }

        @Override
        public void onIceConnectionChange(PeerConnection.IceConnectionState state) {
            Log.i(TAG, "pc " + session.viewerId + " ice: " + state);
        }

        @Override
        public void onIceGatheringChange(PeerConnection.IceGatheringState state) {
            if (state == PeerConnection.IceGatheringState.COMPLETE) {
                session.gatherDone.countDown();
            }
        }

        @Override
        public void onIceConnectionReceivingChange(boolean receiving) {
        }

        @Override
        public void onSignalingChange(PeerConnection.SignalingState state) {
        }

        @Override
        public void onIceCandidatesRemoved(IceCandidate[] candidates) {
        }

        @Override
        public void onAddStream(org.webrtc.MediaStream stream) {
        }

        @Override
        public void onRemoveStream(org.webrtc.MediaStream stream) {
        }

        @Override
        public void onDataChannel(org.webrtc.DataChannel dc) {
        }

        @Override
        public void onRenegotiationNeeded() {
        }

        @Override
        public void onTrack(org.webrtc.RtpTransceiver transceiver) {
        }
    }

    /** setRemoteDescription(offer). */
    private static final class CamSetRemoteObserver implements SdpObserver {
        private final CamPeerSession session;

        CamSetRemoteObserver(CamPeerSession session) {
            this.session = session;
        }

        @Override
        public void onCreateSuccess(SessionDescription sdp) {
        }

        @Override
        public void onSetSuccess() {
            MediaConstraints constraints = new MediaConstraints();
            session.pc.createAnswer(new CamAnswerObserver(session), constraints);
        }

        @Override
        public void onCreateFailure(String error) {
        }

        @Override
        public void onSetFailure(String error) {
            Log.e(TAG, "setRemote failed for " + session.viewerId + ": " + error);
            session.service.notifySessionFailed(session.viewerId, "setRemote: " + error);
        }
    }

    /** createAnswer + setLocalDescription + отправка answer. */
    private static final class CamAnswerObserver implements SdpObserver {
        private final CamPeerSession session;

        CamAnswerObserver(CamPeerSession session) {
            this.session = session;
        }

        @Override
        public void onCreateSuccess(SessionDescription sdp) {
            if (session.pc == null) {
                return;
            }
            session.pc.setLocalDescription(new CamSetLocalObserver(session), sdp);
            if (!session.answerSent) {
                session.answerSent = true;
                session.sendAnswer(sdp);
            }
        }

        @Override
        public void onCreateFailure(String error) {
            Log.e(TAG, "createAnswer failed for " + session.viewerId + ": " + error);
            session.service.notifySessionFailed(session.viewerId, "createAnswer: " + error);
        }

        @Override
        public void onSetSuccess() {
        }

        @Override
        public void onSetFailure(String error) {
            Log.e(TAG, "setLocal failed for " + session.viewerId + ": " + error);
        }
    }

    /** setLocalDescription(answer). */
    private static final class CamSetLocalObserver implements SdpObserver {
        private final CamPeerSession session;

        CamSetLocalObserver(CamPeerSession session) {
            this.session = session;
        }

        @Override
        public void onCreateSuccess(SessionDescription sdp) {
        }

        @Override
        public void onSetSuccess() {
            Log.i(TAG, "answer ready for " + session.viewerId);
        }

        @Override
        public void onCreateFailure(String error) {
        }

        @Override
        public void onSetFailure(String error) {
            Log.w(TAG, "setLocal(answer) failed " + session.viewerId + ": " + error);
        }
    }
}
