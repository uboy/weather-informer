package ru.weather.informer;

import android.util.Log;
import org.json.JSONObject;

import java.net.DatagramPacket;
import java.net.DatagramSocket;
import java.net.InetAddress;
import java.net.SocketTimeoutException;
import java.nio.charset.StandardCharsets;

public class UdpDiscoveryWorker implements Runnable {
    private static final String TAG = "UdpDiscoveryWorker";
    private final InformerServer server;

    public UdpDiscoveryWorker(InformerServer server) {
        this.server = server;
    }

    @Override
    public void run() {
        DatagramSocket socket = null;
        try {
            socket = new DatagramSocket();
            socket.setBroadcast(true);
            socket.setSoTimeout(2000); // 2 sec timeout
            byte[] sendData = "{\"cmd\":\"discover\",\"client\":\"informer-tablet\",\"v\":1}".getBytes(StandardCharsets.UTF_8);
            DatagramPacket sendPacket = new DatagramPacket(
                    sendData, sendData.length,
                    InetAddress.getByName("255.255.255.255"), 8085
            );
            socket.send(sendPacket);
            Log.i(TAG, "Sent UDP discovery broadcast to 255.255.255.255:8085");

            byte[] recvBuf = new byte[2048];
            DatagramPacket recvPacket = new DatagramPacket(recvBuf, recvBuf.length);
            socket.receive(recvPacket);

            String respStr = new String(recvPacket.getData(), 0, recvPacket.getLength(), StandardCharsets.UTF_8);
            Log.i(TAG, "Received UDP discovery response: " + respStr);
            JSONObject resp = new JSONObject(respStr);
            int port = resp.optInt("port", 8085);
            String host = recvPacket.getAddress().getHostAddress();
            String sUrl = "http://" + host + ":" + port;
            if (server != null) {
                server.setDiscoveredServerUrl(sUrl);
            }
            Log.i(TAG, "Discovered weather cache server at: " + sUrl);
        } catch (SocketTimeoutException ste) {
            Log.i(TAG, "UDP discovery timed out after 2s; running in standalone mode");
        } catch (Exception e) {
            Log.w(TAG, "UDP discovery error: " + e.getMessage());
        } finally {
            if (socket != null && !socket.isClosed()) {
                socket.close();
            }
        }
    }
}
