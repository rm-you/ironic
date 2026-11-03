#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import hashlib
from pathlib import Path

import fixtures
from unittest import mock

import sushy

from ironic.common import exception
from ironic.drivers.modules.redfish import firmware
from ironic.drivers.modules.redfish import firmware_utils
from ironic.drivers.modules.redfish import utils
from ironic.tests import base


class MultipartFirmwareTestCase(base.TestCase):

    def setUp(self):
        super().setUp()
        self.interface = firmware.RedfishFirmware()
        self.node = mock.Mock(uuid='test-multipart-node',
                              driver_internal_info={})
        self.service = mock.Mock()
        self.service.multipart_update.return_value.task_monitor_uri = '/task/1'
        self.update = {'component': 'bmc',
                       'url': 'https://images.example/bmc.bin',
                       'method': 'multipart', 'checksum': 'a' * 64,
                       'targets': ['/inventory/BMCImage1'],
                       'oem_parameters': {'ImageType': 'BMC'}}

    def _download(self, node, url, temp_dir=None):
        self.downloaded = Path(temp_dir) / 'firmware.bin'
        self.downloaded.write_bytes(b'firmware')
        return str(self.downloaded)

    @mock.patch.object(firmware_utils, 'cleanup', autospec=True)
    @mock.patch.object(firmware_utils, 'verify_checksum', autospec=True)
    @mock.patch.object(firmware_utils, 'download_to_temp', autospec=True)
    def test_download_verify_push_cleanup(self, download, verify, cleanup):
        download.side_effect = self._download
        calls = mock.Mock()
        calls.attach_mock(download, 'download')
        calls.attach_mock(verify, 'verify')
        calls.attach_mock(self.service.multipart_update, 'push')
        self.interface._submit_multipart_update(
            self.node, self.service, self.update)
        self.assertEqual(['download', 'verify', 'push'],
                         [call[0] for call in calls.mock_calls])
        self.service.multipart_update.assert_called_once_with(
            str(self.downloaded), targets=['/inventory/BMCImage1'],
            oem_parameters={'ImageType': 'BMC'}, force_update=None)
        self.assertEqual('/task/1', self.update['task_monitor'])
        self.assertFalse(self.downloaded.parent.exists())
        cleanup.assert_not_called()

    @mock.patch.object(firmware_utils, 'verify_checksum', autospec=True)
    @mock.patch.object(firmware_utils, 'download_to_temp', autospec=True)
    def test_checksum_failure_prevents_push(self, download, verify):
        download.side_effect = self._download
        verify.side_effect = exception.RedfishError(error='checksum mismatch')
        self.assertRaises(exception.RedfishError,
                          self.interface._submit_multipart_update,
                          self.node, self.service, self.update)
        self.service.multipart_update.assert_not_called()
        self.assertFalse(self.downloaded.parent.exists())

    @mock.patch.object(firmware_utils, 'download_to_temp', autospec=True)
    def test_push_failure_cleans_download(self, download):
        download.side_effect = self._download
        self.update.pop('checksum')
        self.service.multipart_update.side_effect = (
            sushy.exceptions.ConnectionError(
                url='/upload', error='connection lost'))
        self.assertRaises(exception.RedfishError,
                          self.interface._submit_multipart_update,
                          self.node, self.service, self.update)
        self.assertFalse(self.downloaded.parent.exists())

    @mock.patch.object(firmware_utils, 'download_to_temp', autospec=True)
    def test_partial_download_failure_cleans_download(self, download):
        def fail(*args, **kwargs):
            self._download(*args, **kwargs)
            raise exception.RedfishError(error='download failed')

        download.side_effect = fail
        self.assertRaises(exception.RedfishError,
                          self.interface._submit_multipart_update,
                          self.node, self.service, self.update)
        self.service.multipart_update.assert_not_called()
        self.assertFalse(self.downloaded.parent.exists())

    @mock.patch.object(firmware_utils, 'image_service', autospec=True)
    def test_push_preserves_another_staged_image(self, image_service):
        root = Path(self.useFixture(fixtures.TempDir()).path)
        self.config(http_root=str(root / 'http'),
                    http_url='http://conductor.example/', group='deploy')
        previous = root / 'firmware.bin'
        previous.write_bytes(b'previous image')
        staged_url, cleanup = firmware_utils.stage(
            self.node, 'local', str(previous))
        self.node.driver_internal_info['firmware_cleanup'] = [cleanup]
        staged = root / 'http' / 'firmware' / self.node.uuid / previous.name
        self.assertTrue(staged.exists())
        image_service.HttpImageService.return_value.download.side_effect = (
            lambda url, file: file.write(b'new image'))
        self.update['url'] = 'https://images.example/firmware.bin'
        self.update['checksum'] = hashlib.sha256(b'new image').hexdigest()
        self.interface._submit_multipart_update(
            self.node, self.service, self.update)
        self.assertEqual(b'previous image', staged.read_bytes())
        self.assertEqual(b'previous image', previous.read_bytes())
        self.assertIn(self.node.uuid, staged_url)
        self.assertEqual([cleanup],
                         self.node.driver_internal_info['firmware_cleanup'])

    @mock.patch.object(firmware_utils, 'download_to_temp', autospec=True)
    def test_old_sushy_and_missing_uri_fail_before_download(self, download):
        self.assertRaisesRegex(
            exception.RedfishError, 'Installed sushy',
            self.interface._submit_multipart_update,
            self.node, object(), self.update)
        self.service.multipart_http_push_uri = None
        self.assertRaisesRegex(
            exception.RedfishError, 'MultipartHttpPushUri',
            self.interface._submit_multipart_update,
            self.node, self.service, self.update)
        download.assert_not_called()

    @mock.patch.object(utils, 'get_manager', autospec=True)
    @mock.patch.object(utils, 'get_system', autospec=True)
    def test_version_skip_and_downgrade(self, system, manager):
        system.return_value.bios_version = '2.0'
        manager.return_value.firmware_version = '3.0'
        settings = [{'component': 'bios', 'version': '2.0'},
                    {'component': 'bmc', 'version': '3.0'},
                    {'component': 'bios', 'version': '1.0'}]
        self.interface._skip_matching_updates(self.node, settings)
        self.assertEqual([{'component': 'bios', 'version': '1.0'}], settings)

    @mock.patch.object(utils, 'get_system', autospec=True)
    @mock.patch.object(utils, 'get_update_service', autospec=True)
    def test_all_skipped_completes_without_submission(
            self, update_service, system):
        system.return_value.bios_version = '1.0'
        task = mock.Mock(node=self.node)
        self.assertIsNone(self.interface.update(
            task, [{'component': 'bios', 'url': 'https://example/bios.bin',
                    'version': '1.0'}]))
        update_service.assert_not_called()

    def test_schema_multipart_parameters(self):
        firmware_utils.validate_firmware_interface_update_args([self.update])
        for key, value in [('targets', 'not-a-list'),
                           ('oem_parameters', 'not-an-object'),
                           ('method', 'automatic')]:
            update = dict(self.update, **{key: value})
            self.assertRaises(
                exception.InvalidParameterValue,
                firmware_utils.validate_firmware_interface_update_args,
                [update])
        update = dict(self.update, method='simple')
        self.assertRaises(
            exception.InvalidParameterValue,
            firmware_utils.validate_firmware_interface_update_args,
            [update])

    @mock.patch.object(firmware_utils, 'image_service', autospec=True)
    def test_https_and_real_checksums(self, image_service):
        data = b'firmware bytes'
        image_service.HttpImageService.return_value.download.side_effect = (
            lambda url, file: file.write(data))
        try:
            name = firmware_utils.download_to_temp(
                self.node, self.update['url'])
            for algorithm in ('sha1', 'sha256', 'sha512'):
                checksum = hashlib.new(algorithm, data).hexdigest()
                firmware_utils.verify_checksum(self.node, checksum, name)
            self.assertRaises(exception.RedfishError,
                              firmware_utils.verify_checksum, self.node,
                              '0' * 64, name)
        finally:
            firmware_utils.cleanup(self.node)
