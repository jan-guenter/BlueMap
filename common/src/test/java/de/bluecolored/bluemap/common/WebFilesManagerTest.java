/*
 * This file is part of BlueMap, licensed under the MIT License (MIT).
 *
 * Copyright (c) Blue (Lukas Rieger) <https://bluecolored.de>
 * Copyright (c) contributors
 *
 * Permission is hereby granted, free of charge, to any person obtaining a copy
 * of this software and associated documentation files (the "Software"), to deal
 * in the Software without restriction, including without limitation the rights
 * to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
 * copies of the Software, and to permit persons to whom the Software is
 * furnished to do so, subject to the following conditions:
 *
 * The above copyright notice and this permission notice shall be included in
 * all copies or substantial portions of the Software.
 *
 * THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
 * IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
 * FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
 * AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
 * LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
 * OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
 * THE SOFTWARE.
 */
package de.bluecolored.bluemap.common;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.attribute.PosixFileAttributeView;
import java.nio.file.attribute.PosixFilePermissions;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.junit.jupiter.api.Assumptions.assumeTrue;

class WebFilesManagerTest {

    @TempDir
    Path tempDir;

    @Test
    void replacesSettingsAtomicallyWithoutLeavingTemporaryFiles() throws Exception {
        WebFilesManager manager = new WebFilesManager(tempDir);

        manager.saveSettings();
        manager.saveSettings();

        assertTrue(Files.readString(tempDir.resolve("settings.json")).startsWith("{"));
        try (var files = Files.list(tempDir)) {
            assertEquals(1, files.count());
        }
    }

    @Test
    void preservesExistingPosixPermissions() throws Exception {
        assumeTrue(Files.getFileAttributeView(tempDir, PosixFileAttributeView.class) != null);
        Path settingsFile = tempDir.resolve("settings.json");
        Files.writeString(settingsFile, "{}");
        Files.setPosixFilePermissions(settingsFile, PosixFilePermissions.fromString("rw-r-----"));
        var attributesBefore = Files.readAttributes(settingsFile, java.nio.file.attribute.PosixFileAttributes.class);

        new WebFilesManager(tempDir).saveSettings();

        var attributesAfter = Files.readAttributes(settingsFile, java.nio.file.attribute.PosixFileAttributes.class);
        assertEquals(
                PosixFilePermissions.fromString("rw-r-----"),
                attributesAfter.permissions()
        );
        assertEquals(attributesBefore.owner(), attributesAfter.owner());
        assertEquals(attributesBefore.group(), attributesAfter.group());
    }

    @Test
    void createsWebReadableSettingsOnPosixFileSystems() throws Exception {
        assumeTrue(Files.getFileAttributeView(tempDir, PosixFileAttributeView.class) != null);

        new WebFilesManager(tempDir).saveSettings();

        assertEquals(
                PosixFilePermissions.fromString("rw-r--r--"),
                Files.getPosixFilePermissions(tempDir.resolve("settings.json"))
        );
    }

}
