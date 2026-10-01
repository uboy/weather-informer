package ru.weather.informer;

import android.util.Log;
import java.io.IOException;
import java.net.ServerSocket;
import java.net.Socket;
import java.util.concurrent.ExecutorService;

public class ServerAcceptor implements Runnable {
    private static final String TAG = "ServerAcceptor";
    private final InformerServer server;
    private final ServerSocket serverSocket;
    private final ExecutorService threadPool;

    public ServerAcceptor(InformerServer server, ServerSocket serverSocket, ExecutorService threadPool) {
        this.server = server;
        this.serverSocket = serverSocket;
        this.threadPool = threadPool;
    }

    @Override
    public void run() {
        Log.i(TAG, "Informer HTTP Server started on port " + InformerServer.PORT);
        while (server.isRunning()) {
            try {
                Socket socket = serverSocket.accept();
                socket.setSoTimeout(30000);
                threadPool.execute(new ClientHandler(server, socket));
            } catch (IOException e) {
                if (!server.isRunning()) break;
                Log.e(TAG, "Socket accept error: " + e.getMessage());
            }
        }
    }
}
